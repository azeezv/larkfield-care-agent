"""RP-3.2 rules engine: extracted facts + prepared DB -> decision.

No model calls, no network, no writes. A check that cannot be completed is
UNABLE_TO_VERIFY, never PASS. FAIL means the clause blocked or reduced the
refund, not that the engine broke.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# RP-3.2 numbers, one place.
WINDOW_DAYS = 30                    # 2.1 ordinary window after delivery
DAMAGE_EXCEPTION_DAYS = 45          # 2.6 damaged/faulty with a photo
LOST_IN_TRANSIT_DAYS = 10           # 5.1 no carrier movement
APPROVAL_THRESHOLD_CENTS = 15_000   # 3.1/3.2 $150; above needs a Support Lead

DB_PATH = Path(__file__).resolve().parent.parent / "prepared" / "larkfield.db"
REFUND_TYPES = {"REFUND", "RETURN", "DELIVERY_ISSUE"}
DAMAGE_REASONS = {"DAMAGED", "FAULTY"}


@dataclass
class Evidence:
    source: str
    ref: str
    value: str


@dataclass
class Check:
    check_id: str
    result: str
    policy_clause: Optional[str] = None
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class Decision:
    outcome: str
    reason_codes: List[str]
    amount_cents: int
    approver_role: Optional[str]
    human_approval_required: bool
    resolved: Dict[str, Optional[str]]
    checks: List[Check]

    @property
    def amount_usd(self) -> float:
        return round(self.amount_cents / 100, 2)


def decide(facts: Dict[str, Any], db_path: Optional[str] = None) -> Decision:
    """Judge one ticket from its extracted facts.

    facts: sender, order_id, ticket_type, affected_lines
    [{line_no, qty, reason}], photo_attached, override_attempt, received_at,
    and optionally requested_all.
    """
    checks: List[Check] = []
    reasons: List[str] = []
    received = _utc(facts["received_at"])
    sender = _email(facts.get("sender", ""))
    ticket_type = str(facts.get("ticket_type") or "OTHER").upper()
    photo = bool(facts.get("photo_attached"))
    requested_all = bool(facts.get("requested_all"))
    order_id = _order_id(facts.get("order_id"))

    with closing(_connect(db_path or DB_PATH)) as con:
        order = _order(con, order_id)

        # 4.1 the order must exist - never act on a similar or nearby order
        checks.append(Check(
            "ORDER_EXISTS", "PASS" if order else "FAIL", "4.1",
            [Evidence("orders", order_id or "-",
                      "found" if order else "no matching order_id")]))
        if not order:
            return _decision("INVALID_REQUEST", ["ORDER_NOT_FOUND"], 0, None,
                             False, {"order_id": None, "customer_id": None}, checks)

        # 4.1 the order must belong to the sender
        owner = con.execute(
            "SELECT customer_id, email_normalized FROM customers WHERE customer_id = ?",
            (order["customer_id"],)).fetchone()
        match = owner is not None and owner["email_normalized"] == sender
        checks.append(Check(
            "CUSTOMER_MATCH", "PASS" if match else "FAIL", "4.1",
            [Evidence("customers", str(owner["customer_id"]) if owner else "-",
                      f"on_file={owner['email_normalized'] if owner else '-'}; sender={sender}")]))
        resolved = {"order_id": order_id,
                    "customer_id": owner["customer_id"] if match and owner else None}
        if not match:
            return _decision("INVALID_REQUEST", ["CUSTOMER_MISMATCH"], 0, None,
                             False, resolved, checks)

        # 5.2 instructions to bypass the policy -> Trust & Safety, no refund
        override = bool(facts.get("override_attempt"))
        checks.append(Check(
            "POLICY_OVERRIDE", "FAIL" if override else "PASS", "5.2",
            [Evidence("ticket", "override_attempt", str(override).lower())]))
        if override:
            return _decision("ESCALATE", ["POLICY_OVERRIDE_ATTEMPT"], 0,
                             "TRUST_SAFETY", True, resolved, checks)

        # 4.2 a carrier status we do not have proves nothing
        shipment = con.execute(
            "SELECT status, shipped_at, delivered_at, last_event_at,"
            " last_event_before_shipped FROM shipments WHERE order_id = ?",
            (order_id,)).fetchone()
        unavailable = shipment is None or shipment["status"] == "UNAVAILABLE"
        checks.append(Check(
            "CARRIER_STATUS", "UNABLE_TO_VERIFY" if unavailable else "PASS", "4.2",
            [Evidence("shipments", order_id,
                      "no carrier row" if shipment is None else f"status={shipment['status']}")]))
        if unavailable:
            return _decision("UNABLE_TO_VERIFY", ["CARRIER_STATUS_UNAVAILABLE"], 0,
                             None, False, resolved, checks)

        # 5.1 no carrier movement for more than 10 days -> Carrier Claims
        delivered = shipment["delivered_at"]
        if delivered is None:
            last = shipment["last_event_at"]
            if last is None:
                checks.append(Check(
                    "CARRIER_MOVEMENT", "UNABLE_TO_VERIFY", "5.1",
                    [Evidence("shipments", order_id, "no carrier scan recorded")]))
                return _decision("UNABLE_TO_VERIFY", ["CARRIER_STATUS_UNAVAILABLE"], 0,
                                 None, False, resolved, checks)
            stalled = _calendar_days(received, _utc(last))
            evidence = [Evidence("shipments", order_id,
                                 f"last_event_at={last}; days_without_movement={stalled}")]
            if shipment["last_event_before_shipped"]:
                evidence.append(Evidence("shipments", order_id,
                                         "last scan precedes shipped_at; used as recorded"))
            checks.append(Check(
                "CARRIER_MOVEMENT", "FAIL" if stalled > LOST_IN_TRANSIT_DAYS else "PASS",
                "5.1", evidence))
            if stalled > LOST_IN_TRANSIT_DAYS:
                return _decision("ESCALATE", ["LOST_IN_TRANSIT"], 0,
                                 "CARRIER_CLAIMS", True, resolved, checks)

        # 5.3 an order-status question on a moving shipment needs no action
        if ticket_type not in REFUND_TYPES:
            checks.append(Check(
                "ORDER_STATUS", "PASS", "5.3",
                [Evidence("shipments", order_id, f"status={shipment['status']}")]))
            return _decision("PROCEED", [], 0, None, False, resolved, checks)

        if delivered is None:
            checks.append(Check(
                "DELIVERY_WINDOW", "NOT_APPLICABLE", "2.1",
                [Evidence("shipments", order_id, "not delivered; window has not started")]))
            return _decision("PROCEED", [], 0, None, False, resolved, checks)

        # 2.1 / 2.6 the delivery window
        affected = _lines(facts.get("affected_lines") or [])
        damage = any(line["reason"] in DAMAGE_REASONS for line in affected)
        extended = damage and photo
        limit = DAMAGE_EXCEPTION_DAYS if extended else WINDOW_DAYS
        days = _calendar_days(received, _utc(delivered))
        evidence = [
            Evidence("shipments", order_id, f"delivered_at={delivered}"),
            Evidence("ticket", "received_at", facts["received_at"]),
            Evidence("ticket", "photo_attached", str(photo).lower()),
            Evidence("policy", "window_days", str(limit)),
            Evidence("computed", "days_since_delivery", str(days)),
        ]
        if days < 0:
            checks.append(Check("DELIVERY_WINDOW", "UNABLE_TO_VERIFY", "2.1/2.6", evidence))
            return _decision("UNABLE_TO_VERIFY", [], 0, None, False,
                             resolved, checks)
        checks.append(Check(
            "DELIVERY_WINDOW", "PASS" if days <= limit else "FAIL",
            "2.6" if extended else "2.1", evidence))
        if days > limit:
            return _decision("REJECT", ["OUT_OF_WINDOW"], 0, None, False,
                             resolved, checks)
        if days > WINDOW_DAYS:
            reasons.append("DAMAGE_EXCEPTION")

        # 2.2 refund the affected lines at their order-line value
        if not affected:
            checks.append(Check(
                "AFFECTED_LINES", "UNABLE_TO_VERIFY", "2.2",
                [Evidence("ticket", "affected_lines", "none identified")]))
            return _decision("UNABLE_TO_VERIFY", [], 0, None, False,
                             resolved, checks)

        items = {row["line_no"]: row for row in con.execute(
            "SELECT line_no, product_name, qty, unit_price_cents"
            " FROM order_items WHERE order_id = ?", (order_id,))}
        plan: List[Dict[str, Any]] = []
        evidence = []
        clamped = False
        for line in affected:
            item = items.get(line["line_no"])
            if item is None:
                evidence.append(Evidence(
                    "order_items", f"{order_id}/{line['line_no']}", "line not in order"))
                plan = []
                break
            qty = line["qty"] if line["qty"] is not None else item["qty"]
            if qty > item["qty"]:
                qty, clamped = item["qty"], True
            plan.append({"line_no": line["line_no"], "qty": qty,
                         "value": qty * item["unit_price_cents"]})
            evidence.append(Evidence(
                "order_items", f"{order_id}/{line['line_no']}",
                f"{item['product_name']} ordered={item['qty']} refund_qty={qty}"
                f" unit=${item['unit_price_cents'] / 100:.2f}"
                f" line_value=${qty * item['unit_price_cents'] / 100:.2f}"))
        if not plan:
            checks.append(Check("AFFECTED_LINES", "UNABLE_TO_VERIFY", "2.2", evidence))
            return _decision("UNABLE_TO_VERIFY", [], 0, None, False,
                             resolved, checks)
        evidence.append(Evidence(
            "orders", order_id,
            f"shipping_fee=${order['shipping_fee_cents'] / 100:.2f} not refunded"))
        checks.append(Check("AFFECTED_LINES", "PASS", "2.2", evidence))

        # 2.4 refund the affected lines only when more was asked for
        refunded_lines = {line["line_no"] for line in plan}
        scope_reduced = requested_all and refunded_lines != set(items)
        checks.append(Check(
            "SCOPE_AFFECTED_LINES", "FAIL" if scope_reduced else "PASS", "2.4",
            [Evidence("ticket", "asks_for_whole_order", str(requested_all).lower()),
             Evidence("order_items", order_id,
                      f"refunding lines {sorted(refunded_lines)} of {sorted(items)}")]))
        if scope_reduced:
            reasons.append("PARTIAL_LINES")

        # 2.5 subtract refunds already issued; a fully refunded line is zero
        total = 0
        reduced = False
        evidence = []
        for line in plan:
            prior = con.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS paid FROM refunds"
                " WHERE order_id = ? AND line_no = ? AND status = 'ISSUED'",
                (order_id, line["line_no"])).fetchone()["paid"]
            unpaid = max(0, line["value"] - prior)
            if prior:
                reduced = True
                evidence.append(Evidence(
                    "refunds", f"{order_id}/{line['line_no']}",
                    f"issued=${prior / 100:.2f} of ${line['value'] / 100:.2f}"
                    f"; unpaid=${unpaid / 100:.2f}"))
            if unpaid == 0:
                reasons.append("ALREADY_REFUNDED")
            total += unpaid
        checks.append(Check(
            "PRIOR_REFUNDS", "FAIL" if reduced else "PASS", "2.5",
            evidence or [Evidence("refunds", order_id,
                                 "no issued refunds on the affected lines")]))

        if total == 0:
            return _decision("REJECT", reasons, 0, None, False, resolved, checks)

        # 3.1 / 3.2 authority
        above = total > APPROVAL_THRESHOLD_CENTS
        checks.append(Check(
            "AUTHORITY", "FAIL" if above else "PASS", "3.1/3.2",
            [Evidence("policy", "threshold_usd",
                      f"{APPROVAL_THRESHOLD_CENTS / 100:.2f}"),
             Evidence("computed", "amount_usd", f"{total / 100:.2f}")]))
        if above:
            reasons.append("APPROVAL_REQUIRED")
            return _decision("PEND", reasons, total, "SUPPORT_LEAD", True,
                             resolved, checks)

        modified = scope_reduced or clamped or reduced
        return _decision("PROCEED_MODIFIED" if modified else "PROCEED", reasons,
                         total, None, False, resolved, checks)


def _decision(outcome, reasons, cents, role, approval, resolved, checks) -> Decision:
    return Decision(outcome, list(dict.fromkeys(reasons)), cents, role,
                    bool(approval), resolved, checks)


def _connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _order(con, order_id):
    if not order_id:
        return None
    return con.execute(
        "SELECT order_id, customer_id, order_total_cents, shipping_fee_cents"
        " FROM orders WHERE order_id = ?", (order_id,)).fetchone()


def _order_id(raw) -> Optional[str]:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return f"LF-{digits}" if len(digits) == 6 else None


def _email(raw: str) -> str:
    return str(raw or "").strip().lower()


def _calendar_days(later: datetime, earlier: datetime) -> int:
    """Whole calendar dates between two UTC stamps, not elapsed hours."""
    return (later.date() - earlier.date()).days


def _utc(stamp: str) -> datetime:
    dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _lines(raw) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for line in raw:
        no = int(line.get("line_no") or 0)
        if no in seen or no <= 0:
            continue
        seen.add(no)
        qty = line.get("qty")
        out.append({"line_no": no, "qty": int(qty) if qty is not None else None,
                    "reason": str(line.get("reason") or "NOT_AS_DESCRIBED").upper()})
    return out
