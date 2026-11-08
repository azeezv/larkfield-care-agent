"""HTTP API tests with the fake model: no live calls."""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.extract import Extracted, LineClaim, ModelReply
from src.pipeline import Draft

ROOT = Path(__file__).resolve().parents[1]
TICKETS = {t["ticket_id"]: t for t in json.loads(
    (ROOT / "dataset" / "tickets" / "support_inbox_export.json").read_text())}


def fake_extract(order_ref, ticket_type, product):
    calls = []

    def call(prompt):
        calls.append(prompt)
        data = Extracted(order_ref=order_ref, ticket_type=ticket_type,
                         affected_lines=[LineClaim(product=product,
                                                   reason="DAMAGED")])
        return ModelReply(data, "fake-extract", 100, 20)
    call.calls = calls
    return call


def fake_draft(body):
    def call(prompt):
        return ModelReply(Draft(subject="Your Larkfield Home request",
                                body=body), "fake-draft", 50, 10)
    return call


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "api.db")

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, extract, draft):
        return TestClient(create_app(
            db_path=self.db, extract_call=extract, draft_call=draft))

    def test_same_ticket_id_returns_same_run(self):
        extract = fake_extract("LF100201", "REFUND", "Arc floor lamp")
        draft = fake_draft(
            "Hello,\n\nA refund of $129.00 for order LF-100201 is approved."
            "\n\nLarkfield Home Support")
        with self.client(extract, draft) as client:
            first = client.post("/tickets", json=TICKETS["T002"])
            second = client.post("/tickets", json=TICKETS["T002"])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        self.assertEqual(len(extract.calls), 1)

    def test_approving_twice_records_one_ledger_entry(self):
        extract = fake_extract("#100202", "REFUND", "Oak dining chair")
        draft = fake_draft(
            "Hello,\n\nA refund of $185.00 for order LF-100202 needs approval "
            "from our Support Lead.\n\nLarkfield Home Support")
        with self.client(extract, draft) as client:
            created = client.post("/tickets", json=TICKETS["T003"])
            run_id = created.json()["run_id"]
            pending = client.get(f"/runs/{run_id}")
            first = client.post(
                f"/runs/{run_id}/decision",
                json={"decision": "approve", "approver": "lead@example.com"})
            second = client.post(
                f"/runs/{run_id}/decision",
                json={"decision": "approve", "approver": "lead@example.com"})
            ledger = client.get("/ledger")

        self.assertEqual(pending.json()["status"], "PENDING")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "APPROVED")
        self.assertEqual(len(ledger.json()), 1)
        self.assertEqual(ledger.json()[0]["run_id"], run_id)
        self.assertEqual(ledger.json()[0]["action"], "REFUND")
        self.assertEqual(ledger.json()[0]["amount_usd"], 185.0)
        self.assertEqual(ledger.json()[0]["status"], "APPROVED")

    def test_unknown_run_is_404(self):
        extract = fake_extract("LF100201", "REFUND", "Arc floor lamp")
        with self.client(extract, fake_draft("Hello.")) as client:
            self.assertEqual(client.get("/runs/missing").status_code, 404)
            response = client.post(
                "/runs/missing/decision",
                json={"decision": "approve", "approver": "lead@example.com"})
        self.assertEqual(response.status_code, 404)

    def test_bad_body_is_422(self):
        with self.client(fake_extract("LF100201", "REFUND", "Arc floor lamp"),
                         fake_draft("Hello.")) as client:
            response = client.post("/tickets", json={"ticket_id": "T002"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
