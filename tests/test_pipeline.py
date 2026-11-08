"""Pipeline tests with a fake model: no live calls, no network."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import pipeline
from src.extract import Extracted, LineClaim, ModelReply
from src.pipeline import Draft

ROOT = Path(__file__).resolve().parents[1]
TICKETS = {t["ticket_id"]: t for t in json.loads(
    (ROOT / "dataset" / "tickets" / "support_inbox_export.json").read_text())}


def fake_extract(order_ref, ticket_type, product=None, reason="DAMAGED",
                 whole=False, override=False, model="fake-extract", inp=100, out=20):
    lines = [LineClaim(product=product, reason=reason)] if product else []
    data = Extracted(order_ref=order_ref, ticket_type=ticket_type,
                     affected_lines=lines, requests_whole_order=whole,
                     override_attempt=override)
    return lambda prompt: ModelReply(data, model, inp, out)


def fake_draft(body, subject="Your Larkfield Home request", model="fake-draft",
               inp=50, out=10):
    def call(prompt):
        call.prompts.append(prompt)
        return ModelReply(Draft(subject=subject, body=body), model, inp, out)
    call.prompts = []
    return call


def ticket(ticket_id, **overrides):
    data = dict(TICKETS[ticket_id])
    data.update(overrides)
    return data


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "runs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_refund(self):
        draft = fake_draft(
            "Hi Ben,\n\nYour refund of $129.00 for order LF-100201 has been approved "
            "and will go back to your original payment method.\n\nLarkfield Home Support")
        response = pipeline.run_ticket(
            ticket("T002"), checkpoint_path=self.db,
            extract_call=fake_extract("LF100201", "REFUND", "Arc floor lamp"),
            draft_call=draft)

        self.assertIsInstance(response, pipeline.schema.TicketResponse)
        self.assertEqual(response.ticket_id, "T002")
        self.assertEqual(response.ticket_type, "REFUND")
        self.assertEqual(response.outcome, "PROCEED")
        self.assertFalse(response.human_approval_required)
        self.assertIsNone(response.approver_role)
        self.assertEqual(response.resolved.order_id, "LF-100201")
        self.assertEqual(response.resolved.customer_id, "C10001")
        self.assertEqual(response.draft_reply.to, "ben.tanaka1@mail.example")
        self.assertIn("$129.00", response.draft_reply.body)
        self.assertIn("LF-100201", response.draft_reply.body)

        trace = response.trace
        self.assertEqual([c.model for c in trace.model_calls],
                         ["fake-extract", "fake-draft"])
        self.assertEqual([(c.input_tokens, c.output_tokens) for c in trace.model_calls],
                         [(100, 20), (50, 10)])
        self.assertAlmostEqual(trace.cost_usd, 0.00012, places=9)
        self.assertEqual(trace.tools_called,
                         ["order_items", "orders", "customers", "shipments", "refunds"])
        self.assertEqual([(d.document, d.version) for d in trace.documents_used],
                         [("refund_policy_RP-3.2", "3.2")])

    def test_over_150_pends_and_resumes(self):
        draft = fake_draft(
            "Hi Carla,\n\nA refund of $185.00 for order LF-100202 needs approval from "
            "our Support Lead before it can be processed.\n\nLarkfield Home Support")
        state = pipeline.run_state(
            ticket("T003"), checkpoint_path=self.db,
            extract_call=fake_extract("#100202", "REFUND", "Oak dining chair"),
            draft_call=draft)

        self.assertIn("__interrupt__", state)
        response = pipeline.schema.TicketResponse.model_validate(state["response"])
        self.assertEqual(response.outcome, "PEND")
        self.assertTrue(response.human_approval_required)
        self.assertEqual(response.approver_role, "SUPPORT_LEAD")
        self.assertIn("$185.00", response.draft_reply.body)

        with sqlite3.connect(self.db) as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("checkpoints", tables)
        pipeline._GRAPHS.clear()  # reload the saved run from SQLite

        resumed = pipeline.resume_run(response.run_id, "approve",
                                      checkpoint_path=self.db)
        self.assertEqual(resumed.run_id, response.run_id)
        self.assertEqual(resumed.outcome, "PEND")
        self.assertEqual(resumed.draft_reply.body, response.draft_reply.body)
        self.assertEqual(len(draft.prompts), 1)

    def test_escalation_pauses(self):
        draft = fake_draft(
            "Hello,\n\nWe have passed your message to our Trust & Safety team, who "
            "will look into it. No refund has been processed.\n\nLarkfield Home Support")
        state = pipeline.run_state(
            ticket("T020"), checkpoint_path=self.db,
            extract_call=fake_extract("100219", "REFUND", "Wool rug 160x230",
                                      override=True),
            draft_call=draft)

        self.assertIn("__interrupt__", state)
        response = pipeline.schema.TicketResponse.model_validate(state["response"])
        self.assertEqual(response.outcome, "ESCALATE")
        self.assertEqual(response.approver_role, "TRUST_SAFETY")
        self.assertTrue(response.human_approval_required)
        self.assertNotIn("$", response.draft_reply.body)
        self.assertNotIn("418", response.draft_reply.body)

    def test_wrong_sender(self):
        draft = fake_draft(
            "Hello,\n\nWe could not match order LF-100201 to your account. Please reply "
            "from the email address used for the order.\n\nLarkfield Home Support")
        response = pipeline.run_ticket(
            ticket("T002", **{"from": "someone.else@mail.example"}),
            checkpoint_path=self.db,
            extract_call=fake_extract("LF-100201", "REFUND", "Arc floor lamp"),
            draft_call=draft)

        self.assertEqual(response.outcome, "INVALID_REQUEST")
        self.assertEqual(response.reason_codes, ["CUSTOMER_MISMATCH"])
        self.assertEqual(response.resolved.order_id, "LF-100201")
        self.assertIsNone(response.resolved.customer_id)
        self.assertFalse(response.human_approval_required)
        self.assertEqual(response.draft_reply.to, "someone.else@mail.example")
        self.assertNotIn("$", response.draft_reply.body)

    def test_model_failure_is_unable_to_verify(self):
        def boom(prompt):
            raise RuntimeError("quota exhausted")

        response = pipeline.run_ticket(
            ticket("T002"), checkpoint_path=self.db, extract_call=boom,
            draft_call=boom)

        self.assertEqual(response.outcome, "UNABLE_TO_VERIFY")
        self.assertEqual(response.ticket_type, "OTHER")
        self.assertEqual(response.reason_codes, [])
        self.assertFalse(response.human_approval_required)
        self.assertIsNone(response.approver_role)
        self.assertIsNone(response.resolved.order_id)
        self.assertEqual(response.checks[0].check_id, "MODEL_UNAVAILABLE")
        self.assertEqual(response.checks[0].result, "UNABLE_TO_VERIFY")
        self.assertIn("quota exhausted", response.checks[0].evidence[0].value)
        self.assertEqual(response.trace.model_calls, [])
        self.assertEqual(response.trace.cost_usd, 0.0)
        self.assertEqual(response.draft_reply.to, "ben.tanaka1@mail.example")
        self.assertNotIn("$", response.draft_reply.body)

    def test_deepseek_price(self):
        self.assertEqual(pipeline._price("deepseek-flash"), (0.30, 1.20))
        self.assertAlmostEqual(pipeline._cost([{
            "model": "deepseek-flash",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000}]), 1.50)

    def test_bad_draft_retries_then_uses_template(self):
        bad = fake_draft("Hi Ben,\n\nYour refund of $999.00 for order LF-100201 is on "
                         "its way.\n\nLarkfield Home Support")
        response = pipeline.run_ticket(
            ticket("T002"), checkpoint_path=self.db,
            extract_call=fake_extract("LF-100201", "REFUND", "Arc floor lamp"),
            draft_call=bad)

        self.assertEqual(len(bad.prompts), 2)
        self.assertIn("$129.00", response.draft_reply.body)
        self.assertNotIn("$999.00", response.draft_reply.body)
        self.assertEqual(len(response.trace.model_calls), 3)


if __name__ == "__main__":
    unittest.main()
