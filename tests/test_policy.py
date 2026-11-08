"""RP-3.2 rules engine: one test per labelled ticket, plus negatives."""
import json
import unittest
from pathlib import Path

from src import policy

ROOT = Path(__file__).resolve().parents[1]
LABELS = {t["ticket_id"]: t for t in json.loads(
    (ROOT / "dataset" / "tickets" / "eval_seed_labelled.json").read_text())}

# Facts the extraction step produces for each labelled ticket. Order references
# are kept as typed in the email body; the engine normalises them.
CASES = {
    "T001": dict(sender="  ASHA.SHAH0@MAIL.EXAMPLE ", order_id="LF-100200", ticket_type="REFUND",
                 received_at="2026-10-01T09:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T002": dict(sender="ben.tanaka1@mail.example", order_id="LF100201", ticket_type="REFUND",
                 received_at="2026-10-01T10:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T003": dict(sender="carla.ivanova2@mail.example", order_id="#100202", ticket_type="REFUND",
                 received_at="2026-10-02T11:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T004": dict(sender="dev.costa3@mail.example", order_id="100203", ticket_type="REFUND",
                 received_at="2026-10-02T12:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T005": dict(sender="elena.wu4@mail.example", order_id="LF-100204", ticket_type="REFUND",
                 received_at="2026-10-03T13:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T006": dict(sender="  FARID.KHAN5@MAIL.EXAMPLE ", order_id="LF100205", ticket_type="REFUND",
                 received_at="2026-10-03T14:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "WRONG"}]),
    "T008": dict(sender="hiro.wright7@mail.example", order_id="100207", ticket_type="ORDER_STATUS",
                 received_at="2026-10-04T16:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[]),
    "T011": dict(sender="  KAVYA.IYER10@MAIL.EXAMPLE ", order_id="100210", ticket_type="DELIVERY_ISSUE",
                 received_at="2026-10-06T10:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[]),
    "T013": dict(sender="maya.haddad12@mail.example", order_id="LF-100212", ticket_type="REFUND",
                 received_at="2026-10-07T12:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "NOT_DELIVERED"}]),
    "T014": dict(sender="nikhil.miller13@mail.example", order_id="LF100213", ticket_type="RETURN",
                 received_at="2026-10-07T13:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "NOT_AS_DESCRIBED"}]),
    "T018": dict(sender="rahul.reid17@mail.example", order_id="LF100217", ticket_type="REFUND",
                 received_at="2026-10-09T17:00:00+00:00", photo_attached=False, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T019": dict(sender="sara.lee18@mail.example", order_id="#100218", ticket_type="REFUND",
                 received_at="2026-10-10T09:00:00+00:00", photo_attached=False, override_attempt=False,
                 requested_all=True,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T020": dict(sender="tom.mehta19@mail.example", order_id="100219", ticket_type="REFUND",
                 received_at="2026-10-10T10:00:00+00:00", photo_attached=False, override_attempt=True,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"}]),
    "T021": dict(sender="  UMA.NAIR20@MAIL.EXAMPLE ", order_id="LF-100220", ticket_type="RETURN",
                 received_at="2026-10-11T11:00:00+00:00", photo_attached=False, override_attempt=True,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "NOT_AS_DESCRIBED"}]),
    "T026": dict(sender="  ARJUN.ROSSI26@MAIL.EXAMPLE ", order_id="LF-100224", ticket_type="REFUND",
                 received_at="2026-10-13T16:00:00+00:00", photo_attached=True, override_attempt=False,
                 affected_lines=[{"line_no": 1, "qty": 1, "reason": "DAMAGED"},
                                 {"line_no": 2, "qty": 1, "reason": "DAMAGED"}]),
}


class LabelledTickets(unittest.TestCase):
    def test_every_label_has_facts(self):
        self.assertEqual(set(CASES), set(LABELS))


def _ticket_test(ticket_id):
    def test(self):
        expected = LABELS[ticket_id]
        decision = policy.decide(CASES[ticket_id])

        self.assertEqual(decision.outcome, expected["expected_outcome"], decision.reason_codes)
        self.assertEqual(decision.human_approval_required,
                         expected["expected_human_approval_required"])
        self.assertEqual(decision.approver_role, expected["expected_approver_role"])
        if "expected_amount_usd" in expected:
            self.assertAlmostEqual(decision.amount_usd, expected["expected_amount_usd"], places=2)
        for code in expected.get("expected_reason_codes", []):
            self.assertIn(code, decision.reason_codes)

        # Nothing may be proposed without a priced, named clause.
        for check in decision.checks:
            self.assertTrue(check.policy_clause, check.check_id)
            self.assertIn(check.result, {"PASS", "FAIL", "NOT_APPLICABLE", "UNABLE_TO_VERIFY"})
        if (decision.outcome in {"PEND", "PROCEED", "PROCEED_MODIFIED"}
                and CASES[ticket_id]["ticket_type"] in policy.REFUND_TYPES):
            self.assertGreater(decision.amount_cents, 0, decision.reason_codes)
        if decision.outcome == "ESCALATE":
            self.assertTrue(decision.approver_role)
    return test


for _tid in sorted(CASES):
    setattr(LabelledTickets, f"test_{_tid}", _ticket_test(_tid))


class Negatives(unittest.TestCase):
    def test_wrong_sender_is_invalid(self):
        facts = dict(CASES["T002"], sender="someone.else@mail.example")
        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "INVALID_REQUEST")
        self.assertEqual(decision.reason_codes, ["CUSTOMER_MISMATCH"])
        self.assertEqual(decision.amount_cents, 0)
        self.assertIsNone(decision.approver_role)
        self.assertFalse(decision.human_approval_required)
        self.assertIsNone(decision.resolved["customer_id"])

    def test_missing_order_is_invalid(self):
        facts = dict(CASES["T002"], order_id="LF-999999")
        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "INVALID_REQUEST")
        self.assertEqual(decision.reason_codes, ["ORDER_NOT_FOUND"])
        self.assertEqual(decision.amount_cents, 0)
        self.assertIsNone(decision.resolved["order_id"])

    def test_shipping_is_never_refunded(self):
        # T004 line is $45.00 with $9.95 shipping, T005 $38.00 with $9.95.
        self.assertAlmostEqual(policy.decide(CASES["T004"]).amount_usd, 45.00)
        self.assertAlmostEqual(policy.decide(CASES["T005"]).amount_usd, 38.00)

    def test_unverifiable_is_never_pass(self):
        decision = policy.decide(CASES["T013"])
        status = next(c for c in decision.checks if c.check_id == "CARRIER_STATUS")
        self.assertEqual(status.result, "UNABLE_TO_VERIFY")
        self.assertEqual(decision.outcome, "UNABLE_TO_VERIFY")
        self.assertEqual(decision.amount_cents, 0)

    def test_reason_codes_come_from_rp_3_2_only(self):
        allowed = {"OUT_OF_WINDOW", "DAMAGE_EXCEPTION", "PARTIAL_LINES",
                   "ALREADY_REFUNDED", "APPROVAL_REQUIRED", "ORDER_NOT_FOUND",
                   "CUSTOMER_MISMATCH", "CARRIER_STATUS_UNAVAILABLE",
                   "LOST_IN_TRANSIT", "POLICY_OVERRIDE_ATTEMPT"}
        for ticket_id, facts in CASES.items():
            decision = policy.decide(facts)
            self.assertLessEqual(set(decision.reason_codes), allowed, ticket_id)

    def test_damage_exception_needs_photo(self):
        facts = dict(CASES["T001"], photo_attached=False)
        decision = policy.decide(facts)
        self.assertEqual(decision.outcome, "REJECT")
        self.assertEqual(decision.reason_codes, ["OUT_OF_WINDOW"])


class DateEdges(unittest.TestCase):
    """Inclusive limits on calendar dates, proved from both sides."""

    def test_day_30_inside_and_day_31_outside(self):
        base = dict(CASES["T002"], photo_attached=False,
                    affected_lines=[{"line_no": 1, "qty": 1,
                                     "reason": "NOT_AS_DESCRIBED"}])
        inside = policy.decide(dict(base, received_at="2026-10-22T23:30:00+00:00"))
        self.assertEqual(_check(inside, "DELIVERY_WINDOW").result, "PASS")
        self.assertEqual(inside.outcome, "PROCEED")
        outside = policy.decide(dict(base, received_at="2026-10-23T00:30:00+00:00"))
        self.assertEqual(_check(outside, "DELIVERY_WINDOW").result, "FAIL")
        self.assertEqual(outside.reason_codes, ["OUT_OF_WINDOW"])

    def test_day_45_with_photo_inside_and_day_46_outside(self):
        base = CASES["T001"]  # damaged line, photo attached
        inside = policy.decide(dict(base, received_at="2026-10-06T00:30:00+00:00"))
        self.assertEqual(_check(inside, "DELIVERY_WINDOW").result, "PASS")
        self.assertIn("DAMAGE_EXCEPTION", inside.reason_codes)
        self.assertEqual(inside.outcome, "PEND")
        outside = policy.decide(dict(base, received_at="2026-10-07T00:30:00+00:00"))
        self.assertEqual(_check(outside, "DELIVERY_WINDOW").result, "FAIL")
        self.assertEqual(outside.reason_codes, ["OUT_OF_WINDOW"])

    def test_day_10_without_movement_and_day_11(self):
        base = CASES["T011"]          # in transit, last scan 2026-09-20
        moving = policy.decide(dict(base, received_at="2026-09-30T00:30:00+00:00"))
        self.assertEqual(_check(moving, "CARRIER_MOVEMENT").result, "PASS")
        self.assertEqual(moving.reason_codes, [])
        self.assertEqual(moving.outcome, "PROCEED")
        stalled = policy.decide(dict(base, received_at="2026-10-01T00:30:00+00:00"))
        self.assertEqual(_check(stalled, "CARRIER_MOVEMENT").result, "FAIL")
        self.assertEqual(stalled.reason_codes, ["LOST_IN_TRANSIT"])
        self.assertEqual(stalled.approver_role, "CARRIER_CLAIMS")


def _check(decision, check_id):
    return next(c for c in decision.checks if c.check_id == check_id)


if __name__ == "__main__":
    unittest.main()
