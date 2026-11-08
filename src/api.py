"""FastAPI service: propose one decision per support email, never act."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import extract, pipeline

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "prepared" / "api.db"
schema = pipeline.schema


class RunState(schema.TicketResponse):
    status: Literal["PENDING", "APPROVED", "REJECTED", "COMPLETE"]
    decision: Optional[Literal["approve", "reject"]] = None
    approver: Optional[str] = None
    decided_at: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS runs (
            ticket_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            response_json TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT,
            approver TEXT,
            decided_at TEXT,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            ticket_id TEXT NOT NULL,
            action TEXT NOT NULL,
            amount_usd REAL,
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL)""")
        con.commit()


def _state(row: sqlite3.Row) -> RunState:
    data = json.loads(row["response_json"])
    data.update(status=row["status"], decision=row["decision"],
                approver=row["approver"], decided_at=row["decided_at"])
    return RunState.model_validate(data)


def _amount(response) -> float:
    for check in response.checks:
        if check.check_id != "AUTHORITY":
            continue
        for ev in check.evidence:
            if ev.source == "computed" and ev.ref == "amount_usd":
                return round(float(ev.value), 2)
    return 0.0


def _proposed_action(response) -> Optional[tuple[str, Optional[float]]]:
    if response.outcome in ("INVALID_REQUEST", "UNABLE_TO_VERIFY"):
        return None
    if response.outcome == "ESCALATE":
        return "ESCALATION", None
    amount = _amount(response)
    if response.outcome in ("PEND", "PROCEED", "PROCEED_MODIFIED") and amount > 0:
        return "REFUND", amount
    return "REPLY", None


def create_app(*, db_path: Optional[str] = None, extract_call=None,
               draft_call=None) -> FastAPI:
    needs_model = extract_call is None and draft_call is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if needs_model:
            extract.check_config()
        yield

    path = str(Path(db_path or os.environ.get(
        "LARKFIELD_API_DB", DEFAULT_DB)))
    _init_db(path)
    app = FastAPI(title="Larkfield support desk", lifespan=lifespan)
    app.state.db_path = path
    app.state.extract_call = extract_call
    app.state.draft_call = draft_call
    lock = threading.Lock()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/tickets", response_model=schema.TicketResponse)
    def create_ticket(ticket: schema.Ticket):
        with lock:
            with closing(_connect(path)) as con:
                row = con.execute(
                    "SELECT response_json FROM runs WHERE ticket_id = ?",
                    (ticket.ticket_id,)).fetchone()
            if row:
                return schema.TicketResponse.model_validate_json(
                    row["response_json"])

            response = pipeline.run_ticket(
                ticket, checkpoint_path=path,
                extract_call=app.state.extract_call,
                draft_call=app.state.draft_call)
            status = "PENDING" if response.human_approval_required else "COMPLETE"
            created_at = _now()
            try:
                with closing(_connect(path)) as con:
                    with con:
                        con.execute(
                            "INSERT INTO runs (ticket_id, run_id, response_json,"
                            " status, created_at) VALUES (?, ?, ?, ?, ?)",
                            (response.ticket_id, response.run_id,
                             response.model_dump_json(), status, created_at))
                        proposed = _proposed_action(response)
                        if proposed:
                            action, amount = proposed
                            con.execute(
                                "INSERT INTO ledger (run_id, ticket_id, action,"
                                " amount_usd, status, recorded_at)"
                                " VALUES (?, ?, ?, ?, 'PROPOSED', ?)",
                                (response.run_id, response.ticket_id, action,
                                 amount, created_at))
            except sqlite3.IntegrityError:
                with closing(_connect(path)) as con:
                    row = con.execute(
                        "SELECT response_json FROM runs WHERE ticket_id = ?",
                        (ticket.ticket_id,)).fetchone()
                if row is None:
                    raise HTTPException(409, "ticket already being processed")
                return schema.TicketResponse.model_validate_json(
                    row["response_json"])
            return response

    @app.get("/runs/{run_id}", response_model=RunState)
    def get_run(run_id: str):
        with closing(_connect(path)) as con:
            row = con.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "run not found")
        return _state(row)

    @app.post("/runs/{run_id}/decision", response_model=RunState)
    def decide(run_id: str, body: schema.Decision):
        with lock:
            with closing(_connect(path)) as con:
                row = con.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "run not found")
            if row["status"] != "PENDING":
                if (row["status"] == "APPROVED" and body.decision == "approve"
                        or row["status"] == "REJECTED"
                        and body.decision == "reject"):
                    return _state(row)
                raise HTTPException(409, "run already decided")

            try:
                pipeline.resume_run(
                    run_id, body.decision, checkpoint_path=path)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    409, f"run cannot be resumed: {exc}") from exc

            status = "APPROVED" if body.decision == "approve" else "REJECTED"
            decided_at = _now()
            with closing(_connect(path)) as con:
                con.execute("BEGIN IMMEDIATE")
                updated = con.execute(
                    "UPDATE runs SET status = ?, decision = ?, approver = ?,"
                    " decided_at = ? WHERE run_id = ? AND status = 'PENDING'",
                    (status, body.decision, body.approver, decided_at,
                     run_id)).rowcount
                if updated:
                    con.execute(
                        "UPDATE ledger SET status = ?"
                        " WHERE run_id = ? AND status = 'PROPOSED'",
                        ("APPROVED" if body.decision == "approve"
                         else "REJECTED", run_id))
                con.commit()
            with closing(_connect(path)) as con:
                row = con.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _state(row)

    @app.get("/ledger", response_model=List[schema.LedgerEntry])
    def ledger():
        with closing(_connect(path)) as con:
            rows = con.execute(
                "SELECT run_id, ticket_id, action, amount_usd, status,"
                " recorded_at FROM ledger ORDER BY id").fetchall()
        return [schema.LedgerEntry(**dict(row)) for row in rows]

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
