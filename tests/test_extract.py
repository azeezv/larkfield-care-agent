"""Extraction stage against a fake model: no live calls, no network."""
import json
import unittest
from pathlib import Path

from src import extract, policy
from src.extract import Extracted, LineClaim, ModelReply

ROOT = Path(__file__).resolve().parents[1]
TICKETS = {t["ticket_id"]: t for t in json.loads(
    (ROOT / "dataset" / "tickets" / "support_inbox_export.json").read_text())}


def fake(data, model="fake-model", input_tokens=100, output_tokens=20):
    def call(prompt):
        call.prompts.append(prompt)
        return ModelReply(data, model, input_tokens, output_tokens)
    call.prompts = []
    return call


class ExtractTest(unittest.TestCase):
    def test_normal_email(self):
        ticket = TICKETS["T002"]
        model = fake(Extracted(
            order_ref="LF100201", ticket_type="REFUND",
            affected_lines=[LineClaim(product="Arc floor lamp", reason="DAMAGED")]))
        with self.assertLogs("src.extract", "INFO") as logs:
            facts, reply = extract.extract(ticket, call_model=model)

        self.assertEqual(facts["sender"], "ben.tanaka1@mail.example")
        self.assertEqual(facts["order_id"], "LF-100201")
        self.assertEqual(facts["ticket_type"], "REFUND")
        self.assertEqual(facts["affected_lines"],
                         [{"line_no": 1, "qty": 1, "reason": "DAMAGED"}])
        self.assertTrue(facts["photo_attached"])
        self.assertFalse(facts["override_attempt"])
        self.assertFalse(facts["requested_all"])
        self.assertEqual(facts["received_at"], ticket["received_at"])

        logged = logs.output[-1]
        self.assertIn("fake-model", logged)
        self.assertIn("input_tokens=100", logged)
        self.assertIn("output_tokens=20", logged)
        self.assertEqual(reply.model, "fake-model")
        self.assertEqual((reply.input_tokens, reply.output_tokens), (100, 20))

        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "PROCEED")
        self.assertAlmostEqual(decision.amount_usd, 129.00)

    def test_injection_is_reported_not_obeyed(self):
        ticket = dict(TICKETS["T020"])
        ticket["body"] += "\nPhoto of the damage is attached."
        model = fake(Extracted(
            order_ref="100219", ticket_type="REFUND",
            affected_lines=[LineClaim(product="Wool rug 160x230", reason="DAMAGED")],
            override_attempt=True))
        facts, reply = extract.extract(ticket, call_model=model)

        self.assertIn("<email>", model.prompts[0])
        self.assertIn("untrusted", model.prompts[0])
        self.assertTrue(facts["override_attempt"])
        self.assertEqual(facts["sender"], "tom.mehta19@mail.example")
        self.assertFalse(facts["photo_attached"])  # only the body claims a photo

        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "ESCALATE")
        self.assertEqual(decision.approver_role, "TRUST_SAFETY")
        self.assertEqual(decision.amount_cents, 0)  # the body's $418 never becomes an amount

    def test_wrong_sender_is_taken_from_the_header(self):
        ticket = dict(TICKETS["T002"])
        ticket["from"] = "ben.tanaka1@mail.example.attacker.test"
        ticket["body"] += "\nMy email is ben.tanaka1@mail.example, I am the buyer."
        model = fake(Extracted(
            order_ref="LF-100201", ticket_type="REFUND",
            affected_lines=[LineClaim(product="Arc floor lamp", reason="DAMAGED")]))
        facts, reply = extract.extract(ticket, call_model=model)

        self.assertEqual(facts["sender"], "ben.tanaka1@mail.example.attacker.test")
        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "INVALID_REQUEST")
        self.assertEqual(decision.reason_codes, ["CUSTOMER_MISMATCH"])

    def test_no_order_id_means_no_guessing(self):
        ticket = TICKETS["T008"]  # "order order 100207" is not a reference the fake returns
        model = fake(Extracted(
            order_ref=None, ticket_type="ORDER_STATUS",
            affected_lines=[LineClaim(product="Glass vase", reason="NOT_DELIVERED")]))
        facts, reply = extract.extract(ticket, call_model=model)

        self.assertIsNone(facts["order_id"])
        self.assertEqual(facts["affected_lines"], [])  # nothing to match without an order
        self.assertEqual(policy.decide(facts).reason_codes, ["ORDER_NOT_FOUND"])

    def test_whole_order_request(self):
        ticket = TICKETS["T019"]
        model = fake(Extracted(
            order_ref="#100218", ticket_type="REFUND",
            affected_lines=[LineClaim(product="Cushion cover pair", reason="DAMAGED")],
            requests_whole_order=True))
        facts, reply = extract.extract(ticket, call_model=model)

        self.assertEqual(facts["order_id"], "LF-100218")
        self.assertTrue(facts["requested_all"])
        self.assertEqual(facts["affected_lines"],
                         [{"line_no": 1, "qty": 1, "reason": "DAMAGED"}])

        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "PROCEED_MODIFIED")
        self.assertAlmostEqual(decision.amount_usd, 27.50)
        self.assertIn("PARTIAL_LINES", decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
