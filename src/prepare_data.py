#!/usr/bin/env python3
"""Prepare the Larkfield source CSVs into ``prepared/larkfield.db``.

The source directory (``dataset/data/``) is read-only.  The build is pure
standard library, makes no LLM calls, and is safe to re-run: the database is
written to a temporary file and atomically replaced only after every integrity
check passes.

Cleaning rules, one function each:
  1. ``dedupe_orders``       remove *identical* duplicate order rows;
  2. ``parse_money_cents``   parse "$n", "USD n" and bare "n" into integer cents;
  3. ``parse_order_date``    parse ISO or DD/MM/YYYY; ambiguous readings are
                             resolved against that order's ``shipped_at``;
  4. ``normalize_customers`` keep every CRM record, add a trimmed/lowercased
                             email for matching;
  5. ``flag_shipments``      keep rows whose last_event_at precedes shipped_at,
                             add a flag and leave the values untouched.

Usage:
    python src/prepare_data.py [--data-dir dataset/data] [--db prepared/larkfield.db]
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1
CSV_FILES = {
    "customers": "customers.csv",
    "orders": "orders.csv",
    "order_items": "order_items.csv",
    "shipments": "shipments.csv",
    "refunds": "refunds.csv",
}
REQUIRED_COLUMNS = {
    "customers": ["customer_id", "name", "email", "country", "created_at", "loyalty_tier"],
    "orders": ["order_id", "customer_id", "order_date", "status", "Order Total",
               "shipping_fee", "currency", "payment_method"],
    "order_items": ["order_id", "line_no", "sku", "product_name", "qty", "unit_price"],
    "shipments": ["shipment_id", "order_id", "carrier", "tracking_no", "status",
                  "shipped_at", "delivered_at", "last_event_at"],
    "refunds": ["refund_id", "order_id", "line_no", "amount", "status",
                "requested_at", "approved_by"],
}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class DataPrepError(RuntimeError):
    """Raised when a source row cannot be prepared without guessing."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Issue:
    kind: str
    table: str
    record_key: str
    detail: str


@dataclass(frozen=True)
class ParsedDate:
    value: date
    ambiguous: bool
    candidates: tuple[date, ...]
    chosen_format: str
    reason: str


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Prepared:
    customers: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    order_items: list[dict[str, Any]]
    shipments: list[dict[str, Any]]
    refunds: list[dict[str, Any]]
    issues: list[Issue]
    source_counts: dict[str, int]
    stats: dict[str, Any]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "customers": len(self.customers),
            "orders": len(self.orders),
            "order_items": len(self.order_items),
            "shipments": len(self.shipments),
            "refunds": len(self.refunds),
        }


@dataclass
class BuildResult:
    db_path: Path
    data_dir: Path
    counts: dict[str, int]
    source_counts: dict[str, int]
    issues: list[Issue]
    stats: dict[str, Any]
    checks: list[Check] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def read_csv(path: Path, table: str) -> list[dict[str, str]]:
    """Read one CSV without touching it, validating its header."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS[table] if c not in header]
        if missing:
            raise DataPrepError([f"{path.name}: missing columns {missing}"])
        rows = []
        for lineno, row in enumerate(reader, start=2):
            if None in row:
                raise DataPrepError([f"{path.name}:{lineno}: too many fields"])
            if all((v or "").strip() == "" for v in row.values()):
                raise DataPrepError([f"{path.name}:{lineno}: blank row"])
            rows.append({k: (v if v is not None else "") for k, v in row.items()})
    if not rows:
        raise DataPrepError([f"{path.name}: no data rows"])
    return rows


def parse_timestamp(raw: str, where: str = "timestamp") -> datetime:
    """Parse the ISO timestamps used by the carrier feed."""
    s = raw.strip()
    if not s:
        raise ValueError(f"{where}: empty timestamp")
    try:
        return datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"{where}: invalid timestamp {raw!r}") from exc


def parse_iso_date(raw: str, where: str = "date") -> date:
    s = raw.strip()
    if not ISO_DATE_RE.fullmatch(s):
        raise ValueError(f"{where}: expected YYYY-MM-DD, got {raw!r}")
    return date.fromisoformat(s)


# ---------------------------------------------------------------------------
# Rule 1: remove identical duplicate order rows
# ---------------------------------------------------------------------------

def dedupe_orders(rows: Sequence[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    """Drop rows that are byte-for-byte identical on every column.

    A repeated ``order_id`` with *different* content is not deduplicated; it is
    an error because choosing one row would silently lose data.
    """
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["order_id"].strip()].append(row)

    unique: list[dict[str, str]] = []
    removed: list[str] = []
    errors: list[str] = []
    for order_id in sorted(grouped):
        variants: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
        for row in grouped[order_id]:
            variants.setdefault(tuple(sorted(row.items())), row)
        if len(variants) > 1:
            errors.append(
                f"orders.csv: order_id {order_id} has {len(variants)} non-identical rows"
            )
            continue
        unique.append(next(iter(variants.values())))
        removed.extend([order_id] * (len(grouped[order_id]) - 1))
    if errors:
        raise DataPrepError(errors)
    return unique, removed


# ---------------------------------------------------------------------------
# Rule 2: parse order totals to integer cents
# ---------------------------------------------------------------------------

def money_format(raw: str) -> str:
    """Classify the three export formats: $n, USD n, bare n."""
    s = raw.strip()
    if s.startswith("$"):
        return "USD_DOLLAR"
    if s.upper().startswith("USD"):
        return "USD_PREFIX"
    return "PLAIN"


def parse_money_cents(raw: str) -> int:
    """Parse "$310.00", "USD 24.00" or "45.00" into integer cents.

    Negative or non-numeric amounts are rejected rather than guessed at.
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty amount")
    if s.startswith("$"):
        s = s[1:].strip()
    elif s.upper().startswith("USD"):
        s = s[3:].strip()
    s = s.replace(",", "").replace(" ", "")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid amount {raw!r}")
    if value < 0:
        raise ValueError(f"negative amount {raw!r}")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Rule 3: parse order dates, resolving ambiguous DD/MM against shipped_at
# ---------------------------------------------------------------------------

def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_order_date(raw: str, shipped_at: datetime | None = None) -> ParsedDate:
    """Parse an ISO or slash date.

    Slash dates are read as DD/MM/YYYY.  When both DD/MM and MM/DD are valid
    dates (e.g. ``08/10/2026``), the candidates are filtered to those strictly
    before ``shipped_at``:
      * one candidate left -> use it;
      * several left      -> use the latest (closest to shipment);
      * none left         -> use the earliest and mark the reason;
    with no ``shipped_at`` the DD/MM reading is kept.  The chosen date is
    returned with its candidates so the decision can be audited.
    """
    s = raw.strip()
    if ISO_DATE_RE.fullmatch(s):
        return ParsedDate(date.fromisoformat(s), False, (date.fromisoformat(s),), "ISO", "ISO date")

    match = SLASH_DATE_RE.fullmatch(s)
    if not match:
        raise ValueError(f"invalid order_date {raw!r}")
    first, second, year = (int(g) for g in match.groups())

    candidates: dict[date, str] = {}
    for month, day, label in ((second, first, "DD/MM"), (first, second, "MM/DD")):
        parsed = _valid_date(year, month, day)
        if parsed is not None:
            candidates.setdefault(parsed, label)

    if not candidates:
        raise ValueError(f"invalid order_date {raw!r}")
    if len(candidates) == 1:
        value, label = next(iter(candidates.items()))
        return ParsedDate(value, False, (value,), label, "only valid reading")

    ordered = sorted(candidates)
    if shipped_at is None:
        value = next(d for d, label in candidates.items() if label == "DD/MM")
        return ParsedDate(
            value, True, tuple(ordered), "DD/MM",
            "ambiguous; no shipped_at, defaulted to DD/MM",
        )

    before = [
        d for d in ordered
        if datetime.combine(d, time.min) < shipped_at
    ]
    if len(before) == 1:
        value = before[0]
        reason = "only reading before shipped_at"
    elif before:
        value = before[-1]
        reason = "latest reading before shipped_at (tie-break)"
    else:
        value = ordered[0]
        reason = "no reading before shipped_at; earliest fallback"
    return ParsedDate(
        value, True, tuple(ordered), candidates[value], reason,
    )


# ---------------------------------------------------------------------------
# Rule 4: keep every CRM record, normalize emails for matching
# ---------------------------------------------------------------------------

def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def normalize_customers(rows: Sequence[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Keep all CRM rows (both records per name) and add a matching key."""
    out: list[dict[str, str]] = []
    changed = 0
    errors: list[str] = []
    for row in rows:
        email = row["email"].strip()
        normalized = normalize_email(row["email"])
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            errors.append(f"customers.csv: customer {row['customer_id']} has invalid email {row['email']!r}")
            continue
        if normalized != row["email"]:
            changed += 1
        out.append({**row, "email": email, "email_normalized": normalized})
    if errors:
        raise DataPrepError(errors)
    return out, changed


# ---------------------------------------------------------------------------
# Rule 5: keep and flag shipments whose last event precedes shipment
# ---------------------------------------------------------------------------

def flag_shipments(rows: Sequence[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Add ``last_event_before_shipped``; never alter the carrier values."""
    out: list[dict[str, Any]] = []
    anomalies: list[str] = []
    errors: list[str] = []
    for row in rows:
        try:
            shipped = parse_timestamp(row["shipped_at"], f"shipment {row['shipment_id']} shipped_at")
            delivered = (
                parse_timestamp(row["delivered_at"], f"shipment {row['shipment_id']} delivered_at")
                if row["delivered_at"].strip() else None
            )
            last_event = (
                parse_timestamp(row["last_event_at"], f"shipment {row['shipment_id']} last_event_at")
                if row["last_event_at"].strip() else None
            )
            if delivered is not None and delivered < shipped:
                errors.append(
                    f"shipments.csv: {row['shipment_id']} delivered_at precedes shipped_at"
                )
            flagged = int(last_event is not None and last_event < shipped)
        except ValueError as exc:
            errors.append(f"shipments.csv: {exc}")
            continue
        out.append({**row, "last_event_before_shipped": flagged})
        if flagged:
            anomalies.append(row["shipment_id"])
    if errors:
        raise DataPrepError(errors)
    return out, anomalies


# ---------------------------------------------------------------------------
# Build typed rows
# ---------------------------------------------------------------------------

def build_orders(
    rows: Sequence[dict[str, str]],
    shipments_by_order: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    out: list[dict[str, Any]] = []
    date_cases: list[dict[str, Any]] = []
    money_formats: Counter[str] = Counter()
    errors: list[str] = []
    for row in rows:
        order_id = row["order_id"].strip()
        try:
            total_cents = parse_money_cents(row["Order Total"])
            shipping_cents = parse_money_cents(row["shipping_fee"])
        except ValueError as exc:
            errors.append(f"orders.csv: {order_id}: {exc}")
            continue

        shipment = shipments_by_order.get(order_id)
        shipped_at = (
            parse_timestamp(shipment["shipped_at"]) if shipment is not None else None
        )
        try:
            parsed = parse_order_date(row["order_date"], shipped_at)
        except ValueError as exc:
            errors.append(f"orders.csv: {order_id}: {exc}")
            continue

        money_formats[money_format(row["Order Total"])] += 1
        out.append({
            "order_id": order_id,
            "customer_id": row["customer_id"].strip(),
            "order_date": parsed.value.isoformat(),
            "order_date_raw": row["order_date"].strip(),
            "order_date_format": parsed.chosen_format,
            "order_total_cents": total_cents,
            "order_total_raw": row["Order Total"].strip(),
            "shipping_fee_cents": shipping_cents,
            "currency": row["currency"].strip(),
            "payment_method": row["payment_method"].strip(),
            "status": row["status"].strip(),
        })
        if parsed.ambiguous:
            date_cases.append({
                "order_id": order_id,
                "raw": row["order_date"].strip(),
                "chosen": parsed.value.isoformat(),
                "chosen_format": parsed.chosen_format,
                "candidates": [d.isoformat() for d in parsed.candidates],
                "reason": parsed.reason,
            })
    if errors:
        raise DataPrepError(errors)
    out.sort(key=lambda r: r["order_id"])
    return out, date_cases, money_formats


def build_order_items(rows: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        try:
            line_no = int(row["line_no"])
            qty = int(row["qty"])
            unit_cents = parse_money_cents(row["unit_price"])
        except ValueError as exc:
            errors.append(
                f"order_items.csv: {row['order_id']} line {row['line_no']}: {exc}"
            )
            continue
        if line_no < 1 or qty < 1:
            errors.append(
                f"order_items.csv: {row['order_id']} line {row['line_no']}: non-positive line/qty"
            )
            continue
        out.append({
            "order_id": row["order_id"].strip(),
            "line_no": line_no,
            "sku": row["sku"].strip(),
            "product_name": row["product_name"].strip(),
            "qty": qty,
            "unit_price_cents": unit_cents,
        })
    if errors:
        raise DataPrepError(errors)
    out.sort(key=lambda r: (r["order_id"], r["line_no"]))
    return out


def build_refunds(rows: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        try:
            line_no = int(row["line_no"])
            amount_cents = parse_money_cents(row["amount"])
            parse_timestamp(row["requested_at"], f"refund {row['refund_id']} requested_at")
        except ValueError as exc:
            errors.append(f"refunds.csv: {row['refund_id']}: {exc}")
            continue
        out.append({
            "refund_id": row["refund_id"].strip(),
            "order_id": row["order_id"].strip(),
            "line_no": line_no,
            "amount_cents": amount_cents,
            "status": row["status"].strip(),
            "requested_at": row["requested_at"].strip(),
            "approved_by": row["approved_by"].strip(),
        })
    if errors:
        raise DataPrepError(errors)
    out.sort(key=lambda r: r["refund_id"])
    return out


# ---------------------------------------------------------------------------
# Pre-write validation: fail loudly, never drop a row
# ---------------------------------------------------------------------------

def validate(prepared: Prepared) -> None:
    errors: list[str] = []
    customers = {c["customer_id"] for c in prepared.customers}
    orders = {o["order_id"] for o in prepared.orders}
    items = {(i["order_id"], i["line_no"]) for i in prepared.order_items}
    shipments = {s["order_id"] for s in prepared.shipments}
    refunds = prepared.refunds

    for o in prepared.orders:
        if o["customer_id"] not in customers:
            errors.append(f"orders.csv: {o['order_id']} references unknown customer {o['customer_id']}")
        if o["status"] not in {"DELIVERED", "SHIPPED"}:
            errors.append(f"orders.csv: {o['order_id']} invalid status {o['status']!r}")
        if o["currency"] != "USD":
            errors.append(f"orders.csv: {o['order_id']} unexpected currency {o['currency']!r}")
        if o["payment_method"] not in {"card", "paypal"}:
            errors.append(f"orders.csv: {o['order_id']} invalid payment_method {o['payment_method']!r}")
        if not o["order_date"].startswith(("20", "21")):
            errors.append(f"orders.csv: {o['order_id']} implausible order_date {o['order_date']}")

    for i in prepared.order_items:
        if i["order_id"] not in orders:
            errors.append(f"order_items.csv: {i['order_id']} references unknown order")
    for s in prepared.shipments:
        if s["order_id"] not in orders:
            errors.append(f"shipments.csv: {s['shipment_id']} references unknown order {s['order_id']}")
        if s["status"] not in {"DELIVERED", "IN_TRANSIT", "UNAVAILABLE"}:
            errors.append(f"shipments.csv: {s['shipment_id']} invalid status {s['status']!r}")
        if (s["status"] == "DELIVERED") != bool(s["delivered_at"].strip()):
            errors.append(f"shipments.csv: {s['shipment_id']} status/delivered_at disagree")
    for r in refunds:
        if r["order_id"] not in orders:
            errors.append(f"refunds.csv: {r['refund_id']} references unknown order {r['order_id']}")
        if (r["order_id"], r["line_no"]) not in items:
            errors.append(f"refunds.csv: {r['refund_id']} references unknown line "
                          f"{r['order_id']} line {r['line_no']}")
        if r["status"] != "ISSUED":
            errors.append(f"refunds.csv: {r['refund_id']} unexpected status {r['status']!r}")

    # Every order must have items and a exactly one shipment.
    for order_id in orders:
        if not any(i["order_id"] == order_id for i in prepared.order_items):
            errors.append(f"orders.csv: {order_id} has no order_items")
        if order_id not in shipments:
            errors.append(f"orders.csv: {order_id} has no shipment")
    shipment_counts = Counter(s["order_id"] for s in prepared.shipments)
    for order_id, n in shipment_counts.items():
        if n != 1:
            errors.append(f"shipments.csv: {order_id} has {n} shipments, expected 1")

    # Order total must reconcile to its lines and refunds must fit the line.
    item_totals: dict[str, int] = defaultdict(int)
    line_values = {(i["order_id"], i["line_no"]): i["qty"] * i["unit_price_cents"]
                   for i in prepared.order_items}
    for i in prepared.order_items:
        item_totals[i["order_id"]] += i["qty"] * i["unit_price_cents"]
    for o in prepared.orders:
        if item_totals.get(o["order_id"], 0) != o["order_total_cents"]:
            errors.append(
                f"orders.csv: {o['order_id']} total {o['order_total_cents']} "
                f"!= items {item_totals.get(o['order_id'], 0)}"
            )
    for r in refunds:
        value = line_values.get((r["order_id"], r["line_no"]))
        if value is not None and r["amount_cents"] > value:
            errors.append(
                f"refunds.csv: {r['refund_id']} amount {r['amount_cents']} exceeds line value {value}"
            )

    if errors:
        raise DataPrepError(errors)


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE customers (
    customer_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    email            TEXT NOT NULL,
    email_normalized TEXT NOT NULL CHECK (instr(email_normalized, '@') > 1),
    country          TEXT NOT NULL CHECK (length(country) = 2),
    created_at       TEXT NOT NULL,
    loyalty_tier     TEXT NOT NULL CHECK (loyalty_tier IN ('none', 'silver', 'gold'))
);

CREATE INDEX idx_customers_email ON customers (email_normalized);

CREATE TABLE orders (
    order_id          TEXT PRIMARY KEY,
    customer_id       TEXT NOT NULL REFERENCES customers (customer_id),
    order_date        TEXT NOT NULL,
    order_date_raw    TEXT NOT NULL,
    order_date_format TEXT NOT NULL CHECK (order_date_format IN ('ISO', 'DD/MM', 'MM/DD')),
    order_total_cents INTEGER NOT NULL CHECK (order_total_cents >= 0),
    order_total_raw   TEXT NOT NULL,
    shipping_fee_cents INTEGER NOT NULL CHECK (shipping_fee_cents >= 0),
    currency          TEXT NOT NULL CHECK (currency = 'USD'),
    payment_method    TEXT NOT NULL CHECK (payment_method IN ('card', 'paypal')),
    status            TEXT NOT NULL CHECK (status IN ('DELIVERED', 'SHIPPED'))
);

CREATE INDEX idx_orders_customer ON orders (customer_id);

CREATE TABLE order_items (
    order_id         TEXT NOT NULL REFERENCES orders (order_id),
    line_no          INTEGER NOT NULL CHECK (line_no > 0),
    sku              TEXT NOT NULL,
    product_name     TEXT NOT NULL,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE shipments (
    shipment_id              TEXT PRIMARY KEY,
    order_id                 TEXT NOT NULL UNIQUE REFERENCES orders (order_id),
    carrier                  TEXT NOT NULL,
    tracking_no              TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN ('DELIVERED', 'IN_TRANSIT', 'UNAVAILABLE')),
    shipped_at               TEXT NOT NULL,
    delivered_at             TEXT,
    last_event_at            TEXT,
    last_event_before_shipped INTEGER NOT NULL CHECK (last_event_before_shipped IN (0, 1)),
    CHECK ((status = 'DELIVERED') = (delivered_at IS NOT NULL))
);

CREATE TABLE refunds (
    refund_id    TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status       TEXT NOT NULL CHECK (status = 'ISSUED'),
    requested_at TEXT NOT NULL,
    approved_by  TEXT NOT NULL,
    FOREIGN KEY (order_id, line_no) REFERENCES order_items (order_id, line_no)
);

CREATE TABLE data_issues (
    issue_id   INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_key TEXT NOT NULL,
    detail     TEXT NOT NULL
);

CREATE TABLE build_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 1;
"""


# ---------------------------------------------------------------------------
# Prepare and write
# ---------------------------------------------------------------------------

def prepare(data_dir: Path) -> Prepared:
    raw = {table: read_csv(data_dir / name, table) for table, name in CSV_FILES.items()}

    customers, email_changes = normalize_customers(raw["customers"])
    shipments, shipment_anomalies = flag_shipments(raw["shipments"])
    orders_raw, duplicate_order_rows = dedupe_orders(raw["orders"])

    shipments_by_order = {s["order_id"]: s for s in shipments}
    orders, date_cases, money_formats = build_orders(orders_raw, shipments_by_order)
    order_items = build_order_items(raw["order_items"])
    refunds = build_refunds(raw["refunds"])

    issues: list[Issue] = []
    for order_id in duplicate_order_rows:
        issues.append(Issue(
            "DUPLICATE_ORDER_ROW", "orders", order_id,
            "identical duplicate row removed; one row kept",
        ))
    for case in date_cases:
        issues.append(Issue(
            "AMBIGUOUS_ORDER_DATE", "orders", case["order_id"],
            json.dumps({
                "raw": case["raw"],
                "candidates": case["candidates"],
                "chosen": case["chosen"],
                "chosen_format": case["chosen_format"],
                "reason": case["reason"],
            }, sort_keys=True),
        ))
    for shipment_id in shipment_anomalies:
        row = next(s for s in shipments if s["shipment_id"] == shipment_id)
        issues.append(Issue(
            "SHIPMENT_LAST_EVENT_BEFORE_SHIPPED", "shipments", shipment_id,
            f"last_event_at={row['last_event_at']} < shipped_at={row['shipped_at']}; "
            "values kept unchanged, flagged",
        ))

    names = Counter(c["name"].strip() for c in customers)
    stats = {
        "duplicate_order_rows": duplicate_order_rows,
        "date_cases": date_cases,
        "slash_dates": sum(1 for o in orders if o["order_date_format"] in {"DD/MM", "MM/DD"}),
        "ambiguous_dates": date_cases,
        "resolved_by_shipped": [c for c in date_cases if c["reason"].endswith("before shipped_at")],
        "resolved_by_tiebreak": [c for c in date_cases if "tie-break" in c["reason"]],
        "money_formats": dict(money_formats),
        "shipment_anomalies": shipment_anomalies,
        "email_changes": email_changes,
        "customer_rows": len(customers),
        "distinct_names": len(names),
        "names_with_multiple_records": sum(1 for n in names.values() if n > 1),
        "flagged_shipments": len(shipment_anomalies),
    }

    prepared = Prepared(
        customers=customers,
        orders=orders,
        order_items=order_items,
        shipments=shipments,
        refunds=refunds,
        issues=issues,
        source_counts={table: len(rows) for table, rows in raw.items()},
        stats=stats,
    )
    validate(prepared)
    return prepared


def run_checks(conn: sqlite3.Connection, expected: dict[str, int], stats: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []

    for table, count in expected.items():
        actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        checks.append(Check(f"row count {table}", actual == count, f"{actual} (expected {count})"))

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    checks.append(Check("sqlite integrity_check", integrity == "ok", integrity))

    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    checks.append(Check("foreign keys", not fk_rows, f"{len(fk_rows)} orphan row(s)"))

    missing_items = conn.execute(
        "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)"
    ).fetchone()[0]
    checks.append(Check("every order has items", missing_items == 0, f"{missing_items} order(s) without items"))

    bad_shipments = conn.execute(
        "SELECT COUNT(*) FROM orders o WHERE "
        "(SELECT COUNT(*) FROM shipments s WHERE s.order_id = o.order_id) != 1"
    ).fetchone()[0]
    checks.append(Check("one shipment per order", bad_shipments == 0, f"{bad_shipments} order(s) with != 1 shipment"))

    bad_totals = conn.execute(
        "SELECT COUNT(*) FROM orders o JOIN "
        "(SELECT order_id, SUM(qty * unit_price_cents) AS total FROM order_items GROUP BY order_id) i "
        "ON i.order_id = o.order_id WHERE o.order_total_cents != i.total"
    ).fetchone()[0]
    checks.append(Check("order totals reconcile", bad_totals == 0, f"{bad_totals} mismatch(es)"))

    bad_refunds = conn.execute(
        "SELECT COUNT(*) FROM refunds r JOIN order_items i "
        "ON i.order_id = r.order_id AND i.line_no = r.line_no "
        "WHERE r.amount_cents > i.qty * i.unit_price_cents"
    ).fetchone()[0]
    checks.append(Check("refunds fit line value", bad_refunds == 0, f"{bad_refunds} over-refund(s)"))

    bad_delivery = conn.execute(
        "SELECT COUNT(*) FROM shipments WHERE "
        "(status = 'DELIVERED') != (delivered_at IS NOT NULL)"
    ).fetchone()[0]
    checks.append(Check("delivery status/date agree", bad_delivery == 0, f"{bad_delivery} row(s) disagree"))

    dup_emails = conn.execute(
        "SELECT COUNT(*) FROM (SELECT email_normalized FROM customers "
        "GROUP BY email_normalized HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    checks.append(Check("unique normalized emails", dup_emails == 0, f"{dup_emails} duplicate(s)"))

    flagged = conn.execute(
        "SELECT COUNT(*) FROM shipments WHERE last_event_before_shipped = 1"
    ).fetchone()[0]
    checks.append(Check(
        "shipment flags", flagged == stats["flagged_shipments"],
        f"{flagged} flagged (expected {stats['flagged_shipments']})",
    ))

    bad_dates = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_date < '2020-01-01' OR order_date > '2100-01-01'"
    ).fetchone()[0]
    checks.append(Check("plausible order dates", bad_dates == 0, f"{bad_dates} out of range"))

    return checks


def write_database(db_path: Path, prepared: Prepared, data_dir: Path) -> list[Check]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(tmp_path) + suffix).unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executescript(SCHEMA)

        conn.executemany(
            "INSERT INTO customers (customer_id, name, email, email_normalized, country, created_at, loyalty_tier) "
            "VALUES (:customer_id, :name, :email, :email_normalized, :country, :created_at, :loyalty_tier)",
            prepared.customers,
        )
        conn.executemany(
            "INSERT INTO orders (order_id, customer_id, order_date, order_date_raw, order_date_format, "
            "order_total_cents, order_total_raw, shipping_fee_cents, currency, payment_method, status) "
            "VALUES (:order_id, :customer_id, :order_date, :order_date_raw, :order_date_format, "
            ":order_total_cents, :order_total_raw, :shipping_fee_cents, :currency, :payment_method, :status)",
            prepared.orders,
        )
        conn.executemany(
            "INSERT INTO order_items (order_id, line_no, sku, product_name, qty, unit_price_cents) "
            "VALUES (:order_id, :line_no, :sku, :product_name, :qty, :unit_price_cents)",
            prepared.order_items,
        )
        conn.executemany(
            "INSERT INTO shipments (shipment_id, order_id, carrier, tracking_no, status, shipped_at, "
            "delivered_at, last_event_at, last_event_before_shipped) "
            "VALUES (:shipment_id, :order_id, :carrier, :tracking_no, :status, :shipped_at, "
            ":delivered_at, :last_event_at, :last_event_before_shipped)",
            [{
                **s,
                "delivered_at": s["delivered_at"].strip() or None,
                "last_event_at": s["last_event_at"].strip() or None,
            } for s in prepared.shipments],
        )
        conn.executemany(
            "INSERT INTO refunds (refund_id, order_id, line_no, amount_cents, status, requested_at, approved_by) "
            "VALUES (:refund_id, :order_id, :line_no, :amount_cents, :status, :requested_at, :approved_by)",
            prepared.refunds,
        )
        conn.executemany(
            "INSERT INTO data_issues (kind, table_name, record_key, detail) "
            "VALUES (?, ?, ?, ?)",
            [(i.kind, i.table, i.record_key, i.detail) for i in prepared.issues],
        )
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "source_dir": str(Path(data_dir)),
            "built_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source_counts": json.dumps(prepared.source_counts, sort_keys=True),
            "prepared_counts": json.dumps(prepared.counts, sort_keys=True),
        }
        conn.executemany("INSERT INTO build_meta (key, value) VALUES (?, ?)", meta.items())
        conn.commit()

        checks = run_checks(conn, prepared.counts, prepared.stats)
        failed = [c for c in checks if not c.ok]
        if failed:
            raise DataPrepError(
                [f"integrity check failed: {c.name}: {c.detail}" for c in failed]
            )
    except BaseException:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        conn.close()

    os.replace(tmp_path, db_path)
    return checks


def build(db_path: Path, data_dir: Path) -> BuildResult:
    prepared = prepare(data_dir)
    checks = write_database(db_path, prepared, data_dir)
    return BuildResult(
        db_path=Path(db_path),
        data_dir=Path(data_dir),
        counts=prepared.counts,
        source_counts=prepared.source_counts,
        issues=prepared.issues,
        stats=prepared.stats,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def format_summary(result: BuildResult) -> str:
    s = result.stats
    lines: list[str] = []
    lines.append(f"database : {result.db_path}")
    lines.append(f"source   : {result.data_dir}")
    lines.append("")
    lines.append("Rows (source -> prepared)")
    for table in CSV_FILES:
        lines.append(
            f"  {table:<12} {result.source_counts[table]:>3} -> {result.counts[table]:>3}"
        )
    lines.append("")
    lines.append("Cleaning")
    formats = s["money_formats"]
    lines.append(f"  order totals parsed to cents : {sum(formats.values())} "
                 f"($={formats.get('USD_DOLLAR', 0)}, USD={formats.get('USD_PREFIX', 0)}, "
                 f"plain={formats.get('PLAIN', 0)})")
    lines.append(f"  order dates parsed           : {result.counts['orders']} "
                 f"({s['slash_dates']} slash-format, {len(s['ambiguous_dates'])} ambiguous)")
    for case in s["ambiguous_dates"]:
        lines.append(
            f"      {case['order_id']} {case['raw']} -> {case['chosen']} "
            f"({case['chosen_format']}); {case['reason']}"
        )
    lines.append(f"  CRM records kept             : {s['customer_rows']} rows / "
                 f"{s['distinct_names']} names; {s['names_with_multiple_records']} names have 2 records")
    lines.append(f"  emails normalized            : {s['email_changes']} changed")
    lines.append("")
    lines.append("Problems (logged in data_issues)")
    lines.append(f"  identical duplicate order rows removed : {len(s['duplicate_order_rows'])}"
                 + (f"  [{', '.join(s['duplicate_order_rows'])}]" if s["duplicate_order_rows"] else ""))
    lines.append(f"  ambiguous order dates                  : {len(s['ambiguous_dates'])}"
                 f"  (resolved by shipped_at: {len(s['resolved_by_shipped'])}, "
                 f"tie-break: {len(s['resolved_by_tiebreak'])})")
    lines.append(f"  last_event_at before shipped_at        : {s['flagged_shipments']}"
                 + (f"  [{', '.join(s['shipment_anomalies'])}]" if s["shipment_anomalies"] else ""))
    lines.append("")
    lines.append("Integrity checks")
    failed = [c for c in result.checks if not c.ok]
    lines.append(f"  {len(result.checks) - len(failed)}/{len(result.checks)} passed")
    for check in result.checks:
        lines.append(f"  [{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=root / "dataset" / "data")
    parser.add_argument("--db", type=Path, default=root / "prepared" / "larkfield.db")
    args = parser.parse_args(argv)
    try:
        result = build(args.db, args.data_dir)
    except DataPrepError as exc:
        print("data preparation failed:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
