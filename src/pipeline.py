"""LangGraph pipeline: extract -> decide -> draft -> response. No HTTP yet.

A PEND or ESCALATE run stops at `await_human`; LangGraph saves its state in
SQLite, and `resume_run()` continues it. Drafts are never sent.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from . import extract, policy

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_DB = Path(os.environ.get("CHECKPOINT_DB", ROOT / "prepared" / "runs.db"))
DOCUMENTS = [{"document": "refund_policy_RP-3.2", "version": "3.2"}]

# USD per million tokens: one price table, overridable by env config.
PRICE_TABLE = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "deepseek-flash": (0.30, 1.20),
}
DEFAULT_PRICE = (0.30, 2.50)


def _load_schema():
    path = ROOT / "dataset" / "schema" / "response_schema.py"
    spec = importlib.util.spec_from_file_location("larkfield_response_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = _load_schema()

DRAFT_INSTRUCTION = """\
You write one short, plain reply from Larkfield Home support to a customer.
Use only the fields given: outcome, refund amount, order id, reason codes and
approver role. State the refund amount exactly as given; if it says "none",
state no amount at all. Mention the order id only if one is given, and never a
different one. Explain the outcome in plain language - do not print reason codes
or mention internal policy numbers. Never promise action that is not shown.
Sign the email "Larkfield Home Support" and address the customer by no name.
"""


class Draft(BaseModel):
    subject: str
    body: str


class State(TypedDict, total=False):
    ticket: Dict[str, Any]
    facts: Dict[str, Any]
    decision: Dict[str, Any]
    draft: Dict[str, str]
    model_calls: List[Dict[str, Any]]
    tools_called: List[str]
    response: Dict[str, Any]
    error: str


# --- nodes -----------------------------------------------------------------

def _extract_node(state: State, config) -> Dict[str, Any]:
    ticket = state["ticket"]
    cfg = config["configurable"]
    try:
        facts, reply = extract.extract(
            ticket, db_path=cfg.get("db_path"), call_model=cfg.get("extract_call"))
    except Exception as exc:  # never guess facts
        log.warning("extract failed ticket=%s: %s", ticket.get("ticket_id"), exc)
        return {"error": f"{type(exc).__name__}: {exc}", "facts": {}}
    return {"facts": facts, "error": "",
            "model_calls": _calls(state) + [_call_dict(reply)]}


def _decide_node(state: State, config) -> Dict[str, Any]:
    if state.get("error"):
        return {"decision": _unverified(state["error"]), "tools_called": []}
    decision = policy.decide(
        state["facts"], db_path=config["configurable"].get("db_path"))
    return {"decision": asdict(decision),
            "tools_called": _tools_called(state["facts"], decision)}


def _draft_node(state: State, config) -> Dict[str, Any]:
    decision = state["decision"]
    ticket = state["ticket"]
    facts = state.get("facts") or {}
    sender = facts.get("sender") or extract.sender(ticket)
    calls = _calls(state)

    if state.get("error"):  # the model is unavailable; use the plain template
        return {"draft": _template(decision), "model_calls": calls}

    cfg = config["configurable"]
    call = cfg.get("draft_call") or (
        lambda prompt: extract.generate(prompt, Draft, DRAFT_INSTRUCTION))
    prompt = _draft_prompt(decision, sender)
    problems: List[str] = []
    for _ in range(2):
        try:
            reply = call(prompt if not problems else _retry_prompt(prompt, problems))
        except Exception as exc:
            log.warning("draft failed ticket=%s: %s", ticket.get("ticket_id"), exc)
            break
        calls.append(_call_dict(reply))
        problems = _mismatches(reply.data.body, decision)
        if not problems:
            return {"draft": {"subject": reply.data.subject,
                              "body": reply.data.body}, "model_calls": calls}
    log.warning("draft rejected ticket=%s: %s", ticket.get("ticket_id"), problems)
    return {"draft": _template(decision), "model_calls": calls}


def _build_node(state: State, config) -> Dict[str, Any]:
    decision = state["decision"]
    facts = state.get("facts") or {}
    ticket = state["ticket"]
    draft = state["draft"]
    calls = _calls(state)
    sender = facts.get("sender") or extract.sender(ticket)

    response = schema.TicketResponse(
        ticket_id=ticket.get("ticket_id", ""),
        run_id=config["configurable"]["run_id"],
        ticket_type=facts.get("ticket_type", "OTHER"),
        resolved=schema.Resolved(**decision["resolved"]),
        outcome=decision["outcome"],
        reason_codes=decision["reason_codes"],
        checks=[schema.Check(
            check_id=c["check_id"], result=c["result"],
            evidence=[schema.Evidence(**e) for e in c["evidence"]],
            policy_clause=c.get("policy_clause")) for c in decision["checks"]],
        human_approval_required=decision["human_approval_required"],
        approver_role=decision["approver_role"],
        draft_reply=schema.DraftReply(
            to=sender, subject=draft["subject"], body=draft["body"]),
        trace=schema.Trace(
            model_calls=[schema.ModelCall(**c) for c in calls],
            tools_called=state.get("tools_called", []),
            documents_used=[schema.DocumentUsed(**d) for d in DOCUMENTS],
            cost_usd=_cost(calls)),
    )
    return {"response": response.model_dump()}


def _await_human_node(state: State, config) -> Dict[str, Any]:
    decision = state["decision"]
    interrupt({
        "run_id": config["configurable"]["run_id"],
        "outcome": decision["outcome"],
        "approver_role": decision["approver_role"],
        "amount_usd": round(decision["amount_cents"] / 100, 2),
    })
    return {}


# --- graph and entry points ------------------------------------------------

def _route_after_build(state: State) -> str:
    return "await_human" if state["decision"]["human_approval_required"] else END


def _build_graph():
    graph = StateGraph(State)
    graph.add_node("extract", _extract_node)
    graph.add_node("decide", _decide_node)
    graph.add_node("draft", _draft_node)
    graph.add_node("build", _build_node)
    graph.add_node("await_human", _await_human_node)
    graph.add_edge(START, "extract")
    graph.add_edge("extract", "decide")
    graph.add_edge("decide", "draft")
    graph.add_edge("draft", "build")
    graph.add_conditional_edges("build", _route_after_build,
                                {"await_human": "await_human", END: END})
    graph.add_edge("await_human", END)
    return graph


_GRAPHS: Dict[str, Any] = {}


def _graph(checkpoint_path: Optional[str] = None):
    path = str(Path(checkpoint_path or CHECKPOINT_DB))
    if path not in _GRAPHS:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
        saver.setup()
        _GRAPHS[path] = _build_graph().compile(checkpointer=saver)
    return _GRAPHS[path]


def run_state(ticket: Any, *, run_id: Optional[str] = None,
              db_path: Optional[str] = None, checkpoint_path: Optional[str] = None,
              extract_call=None, draft_call=None) -> Dict[str, Any]:
    """Run the graph and return its state (including `__interrupt__` if paused)."""
    run_id = run_id or uuid.uuid4().hex
    config = {"configurable": {"thread_id": run_id, "run_id": run_id,
                               "db_path": db_path, "extract_call": extract_call,
                               "draft_call": draft_call}}
    return _graph(checkpoint_path).invoke({"ticket": _ticket(ticket)}, config)


def run_ticket(ticket: Any, **kwargs) -> Any:
    """Run one ticket and return the validated TicketResponse."""
    state = run_state(ticket, **kwargs)
    return schema.TicketResponse.model_validate(state["response"])


def resume_run(run_id: str, decision: Optional[str] = None,
               *, checkpoint_path: Optional[str] = None) -> Any:
    """Continue a paused run with a human decision."""
    config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
    state = _graph(checkpoint_path).invoke(Command(resume=decision), config)
    return schema.TicketResponse.model_validate(state["response"])


# --- draft helpers ---------------------------------------------------------

AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
ORDER_RE = re.compile(r"LF-\d{6}", re.IGNORECASE)


def _draft_prompt(decision: Dict[str, Any], sender: str) -> str:
    return (
        f"Customer: {sender}\n"
        f"Outcome: {decision['outcome']}\n"
        f"Refund amount: {_money(decision['amount_cents'])}\n"
        f"Order id: {(decision['resolved'] or {}).get('order_id') or 'none'}\n"
        f"Reason codes: {', '.join(decision['reason_codes']) or 'none'}\n"
        f"Approver role: {decision['approver_role'] or 'none'}\n"
    )


def _retry_prompt(prompt: str, problems: List[str]) -> str:
    return (prompt + "\nThe previous draft was rejected: "
            + "; ".join(problems) + ". Rewrite it and fix those points.")


def _mismatches(body: str, decision: Dict[str, Any]) -> List[str]:
    """The reply may only carry the decision's amount and order id."""
    problems = []
    if not body.strip():
        problems.append("the body is empty")
    expected = decision["amount_cents"] / 100
    amounts = {round(float(a.replace(",", "")), 2) for a in AMOUNT_RE.findall(body)}
    if expected and amounts != {expected}:
        problems.append(f"the refund must be exactly ${expected:.2f} and no other amount")
    elif not expected and amounts:
        problems.append("no amount may be stated")
    order = (decision["resolved"] or {}).get("order_id")
    ids = {i.upper() for i in ORDER_RE.findall(body)}
    if order and ids - {order}:
        problems.append(f"the only order id allowed is {order}")
    elif not order and ids:
        problems.append("no order id may be stated")
    return problems


def _template(decision: Dict[str, Any]) -> Dict[str, str]:
    outcome = decision["outcome"]
    money = _money(decision["amount_cents"])
    order = (decision["resolved"] or {}).get("order_id")
    where = f" for order {order}" if order else ""
    role = decision["approver_role"]
    if outcome in ("PROCEED", "PROCEED_MODIFIED"):
        body = (f"Hello,\n\nWe have approved a refund of {money}{where}. It will be "
                "returned to your original payment method. Shipping charges are not "
                "included in refunds.\n\nLarkfield Home Support")
    elif outcome == "PEND":
        body = (f"Hello,\n\nWe have reviewed your request{where}. A refund of {money} "
                "needs approval from our Support Lead before it can be processed. We "
                "will write to you once that review is complete.\n\nLarkfield Home Support")
    elif outcome == "ESCALATE":
        team = {"TRUST_SAFETY": "Trust & Safety",
                "CARRIER_CLAIMS": "Carrier Claims"}.get(role, "our team")
        body = (f"Hello,\n\nWe have passed your message{where} to {team}, who will look "
                "into it. No refund has been processed.\n\nLarkfield Home Support")
    elif outcome == "REJECT":
        why = {"OUT_OF_WINDOW": "outside the 30-day refund window"}.get(
            (decision["reason_codes"] or [""])[0], "not eligible under our refund policy")
        body = (f"Hello,\n\nWe have checked your request{where} against our refund policy. "
                f"Unfortunately it is {why}, so we cannot offer a refund."
                "\n\nLarkfield Home Support")
    elif outcome == "INVALID_REQUEST":
        body = ("Hello,\n\nWe could not match this request to an order on your account. "
                "Please reply from the email address used for the order, or send us the "
                "order number.\n\nLarkfield Home Support")
    else:  # UNABLE_TO_VERIFY
        body = ("Hello,\n\nThank you for your message. We are checking the details of "
                "your request and will come back to you as soon as we can. No refund has "
                "been processed yet.\n\nLarkfield Home Support")
    return {"subject": "Your Larkfield Home request", "body": body}


# --- small helpers ---------------------------------------------------------

def _ticket(ticket: Any) -> Dict[str, Any]:
    if isinstance(ticket, dict):
        return dict(ticket)
    return ticket.model_dump(by_alias=True)


def _calls(state: State) -> List[Dict[str, Any]]:
    return list(state.get("model_calls") or [])


def _call_dict(reply) -> Dict[str, Any]:
    return {"model": reply.model, "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens}


def _money(cents: int) -> str:
    return f"${cents / 100:.2f}" if cents else "none"


def _price(model: str):
    pin, pout = os.environ.get("MODEL_PRICE_INPUT"), os.environ.get("MODEL_PRICE_OUTPUT")
    if pin and pout:
        return float(pin), float(pout)
    for name, price in PRICE_TABLE.items():
        if str(model).startswith(name):
            return price
    return DEFAULT_PRICE


def _cost(calls: List[Dict[str, Any]]) -> float:
    total = 0.0
    for call in calls:
        pin, pout = _price(call["model"])
        total += (call["input_tokens"] * pin
                  + call["output_tokens"] * pout) / 1_000_000
    return round(total, 6)


CHECK_TOOLS = {"ORDER_EXISTS": "orders", "CUSTOMER_MATCH": "customers",
               "CARRIER_STATUS": "shipments", "CARRIER_MOVEMENT": "shipments",
               "AFFECTED_LINES": "order_items", "PRIOR_REFUNDS": "refunds"}


def _tools_called(facts: Dict[str, Any], decision) -> List[str]:
    names: List[str] = []
    if facts.get("affected_lines"):
        names.append("order_items")
    for check in decision.checks:
        table = CHECK_TOOLS.get(check.check_id)
        if table and table not in names:
            names.append(table)
    return names


def _unverified(error: str) -> Dict[str, Any]:
    return {
        "outcome": "UNABLE_TO_VERIFY",
        "reason_codes": [],
        "amount_cents": 0,
        "approver_role": None,
        "human_approval_required": False,
        "resolved": {"order_id": None, "customer_id": None},
        "checks": [{
            "check_id": "MODEL_UNAVAILABLE",
            "result": "UNABLE_TO_VERIFY",
            "policy_clause": None,
            "evidence": [{"source": "model",
                          "ref": os.environ.get("MODEL_NAME", "gemini"),
                          "value": error}],
        }],
    }
