"""Unit tests for the data-prep rules and one end-to-end build."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prepare_data import (  # noqa: E402
    DataPrepError,
    build,
    dedupe_orders,
    flag_shipments,
    money_format,
    normalize_customers,
    parse_money_cents,
    parse_order_date,
)

DATA_DIR = ROOT / "dataset" / "data"


class MoneyTests(unittest.TestCase):
    def test_three_export_formats(self):
        self.assertEqual(parse_money_cents("$310.00"), 31000)
        self.assertEqual(parse_money_cents("USD 24.00"), 2400)
        self.assertEqual(parse_money_cents("45.00"), 4500)
        self.assertEqual(parse_money_cents("$1,234.56"), 123456)

    def test_format_labels(self):
        self.assertEqual(money_format("$310.00"), "USD_DOLLAR")
        self.assertEqual(money_format("USD 24.00"), "USD_PREFIX")
        self.assertEqual(money_format("45.00"), "PLAIN")

    def test_rejects_bad_money(self):
        for raw in ("", "abc", "-1.00", "$"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_money_cents(raw)


class OrderDateTests(unittest.TestCase):
    def test_iso(self):
        parsed = parse_order_date("2026-10-01")
        self.assertEqual(parsed.value, date(2026, 10, 1))
        self.assertFalse(parsed.ambiguous)

    def test_unambiguous_slash_is_dd_mm(self):
        parsed = parse_order_date("26/08/2026")
        self.assertEqual(parsed.value, date(2026, 8, 26))
        self.assertEqual(parsed.chosen_format, "DD/MM")
        self.assertFalse(parsed.ambiguous)

    def test_ambiguous_uses_only_reading_before_shipped(self):
        # 08/10/2026: DD/MM = Oct 8 (after shipment), MM/DD = Aug 10 (before).
        parsed = parse_order_date("08/10/2026", datetime(2026, 9, 30, 6, 0))
        self.assertEqual(parsed.value, date(2026, 8, 10))
        self.assertEqual(parsed.chosen_format, "MM/DD")
        self.assertTrue(parsed.ambiguous)

    def test_ambiguous_tie_breaks_to_latest_before_shipped(self):
        # Both Oct 8 and Aug 10 are before Oct 10; choose the latest.
        parsed = parse_order_date("08/10/2026", datetime(2026, 10, 10, 11, 0))
        self.assertEqual(parsed.value, date(2026, 10, 8))
        self.assertEqual(parsed.chosen_format, "DD/MM")

    def test_ambiguous_without_shipment_defaults_to_dd_mm(self):
        parsed = parse_order_date("06/10/2026")
        self.assertEqual(parsed.value, date(2026, 10, 6))
        self.assertIn("DD/MM", parsed.reason)

    def test_invalid_date_raises(self):
        with self.assertRaises(ValueError):
            parse_order_date("not-a-date")


class DedupeTests(unittest.TestCase):
    def test_identical_rows_are_removed_and_counted(self):
        row = {"order_id": "LF-1", "customer_id": "C1", "Order Total": "10.00"}
        unique, removed = dedupe_orders([row, dict(row), {"order_id": "LF-2", "customer_id": "C2",
                                                          "Order Total": "20.00"}])
        self.assertEqual([r["order_id"] for r in unique], ["LF-1", "LF-2"])
        self.assertEqual(removed, ["LF-1"])

    def test_same_key_different_content_is_an_error(self):
        with self.assertRaises(DataPrepError):
            dedupe_orders([
                {"order_id": "LF-1", "customer_id": "C1", "Order Total": "10.00"},
                {"order_id": "LF-1", "customer_id": "C9", "Order Total": "10.00"},
            ])


class CustomerTests(unittest.TestCase):
    def test_keeps_both_records_and_normalizes_email(self):
        rows = [
            {"customer_id": "C1", "name": "Asha Shah", "email": " Asha.Shah0@Mail.Example ",
             "country": "US", "created_at": "2025-09-06", "loyalty_tier": "silver"},
            {"customer_id": "C2", "name": "Asha Shah", "email": "asha.shah30@mail.example",
             "country": "US", "created_at": "2026-06-05", "loyalty_tier": "none"},
        ]
        out, changed = normalize_customers(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual([r["customer_id"] for r in out], ["C1", "C2"])
        self.assertEqual(out[0]["email_normalized"], "asha.shah0@mail.example")
        self.assertEqual(out[0]["email"], "Asha.Shah0@Mail.Example")
        self.assertEqual(changed, 1)


class ShipmentFlagTests(unittest.TestCase):
    def test_flags_without_changing_values(self):
        rows = [
            {"shipment_id": "S1", "order_id": "O1", "carrier": "X", "tracking_no": "T1",
             "status": "IN_TRANSIT", "shipped_at": "2026-10-03T10:00",
             "delivered_at": "", "last_event_at": "2026-09-20T10:00"},
            {"shipment_id": "S2", "order_id": "O2", "carrier": "X", "tracking_no": "T2",
             "status": "DELIVERED", "shipped_at": "2026-10-01T09:00",
             "delivered_at": "2026-10-03T09:00", "last_event_at": "2026-10-03T09:00"},
            {"shipment_id": "S3", "order_id": "O3", "carrier": "X", "tracking_no": "T3",
             "status": "UNAVAILABLE", "shipped_at": "2026-10-03T12:00",
             "delivered_at": "", "last_event_at": ""},
        ]
        out, anomalies = flag_shipments(rows)
        self.assertEqual(anomalies, ["S1"])
        self.assertEqual(out[0]["last_event_before_shipped"], 1)
        self.assertEqual(out[1]["last_event_before_shipped"], 0)
        self.assertEqual(out[2]["last_event_before_shipped"], 0)
        self.assertEqual(out[0]["last_event_at"], "2026-09-20T10:00")
        self.assertEqual(out[0]["shipped_at"], "2026-10-03T10:00")


class EndToEndTests(unittest.TestCase):
    def _hash_dir(self) -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(DATA_DIR.glob("*.csv"))
        }

    def test_build_counts_issues_and_rerun(self):
        before = self._hash_dir()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "larkfield.db"
            first = build(db, DATA_DIR)
            second = build(db, DATA_DIR)  # safe to re-run

            self.assertEqual(first.counts, {
                "customers": 53, "orders": 47, "order_items": 57,
                "shipments": 47, "refunds": 3,
            })
            self.assertEqual(first.counts, second.counts)
            self.assertTrue(all(c.ok for c in first.checks))
            self.assertTrue(all(c.ok for c in second.checks))

            issues = first.issues
            self.assertEqual(sum(i.kind == "DUPLICATE_ORDER_ROW" for i in issues), 2)
            self.assertEqual(sum(i.kind == "AMBIGUOUS_ORDER_DATE" for i in issues), 3)
            self.assertEqual(sum(i.kind == "SHIPMENT_LAST_EVENT_BEFORE_SHIPPED" for i in issues), 4)

            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM data_issues").fetchone()[0], 9)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM build_meta").fetchone()[0], 5)

                # Both CRM records survive; emails are normalized.
                asha = conn.execute(
                    "SELECT customer_id, email_normalized FROM customers "
                    "WHERE name = 'Asha Shah' ORDER BY customer_id"
                ).fetchall()
                self.assertEqual([r[0] for r in asha], ["C10000", "C10030"])
                self.assertTrue(all(r[1] == r[1].strip().lower() for r in asha))

                # The three ambiguous dates were resolved as intended.
                dates = dict(conn.execute(
                    "SELECT order_id, order_date FROM orders WHERE order_id IN "
                    "('LF-100221', 'LF-100230', 'LF-100235')"
                ).fetchall())
                self.assertEqual(dates, {
                    "LF-100221": "2026-08-10",
                    "LF-100230": "2026-10-06",
                    "LF-100235": "2026-10-08",
                })

                # Money is integer cents and the flagged shipment values are unchanged.
                self.assertEqual(conn.execute(
                    "SELECT order_total_cents, order_total_raw FROM orders WHERE order_id = 'LF-100239'"
                ).fetchone(), (18500, "USD 185.00"))
                self.assertEqual(conn.execute(
                    "SELECT last_event_before_shipped FROM shipments WHERE shipment_id = 'SHP-5010'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT last_event_at FROM shipments WHERE shipment_id = 'SHP-5010'"
                ).fetchone()[0], "2026-09-20T10:00")
            finally:
                conn.close()

        self.assertEqual(before, self._hash_dir(), "dataset/data must not change")


if __name__ == "__main__":
    unittest.main()
