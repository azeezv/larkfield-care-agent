"""Run the labelled evaluation with the real model. Saves a timestamped report."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.pipeline import run_ticket  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
RESULTS = HERE / "results"
DELAY = float(os.environ.get("EVAL_DELAY_SECONDS", "5"))
RETRIES = int(os.environ.get("EVAL_MAX_RETRIES", "4"))
RETRY_WAIT = float(os.environ.get("EVAL_RETRY_SECONDS", "20"))


def _daily_quota_exhausted(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".upper()
    return any(mark in text for mark in (
        "DAILY QUOTA", "DAILY_LIMIT", "DAILY LIMIT", "PERDAY", "PER DAY"))


def _rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".upper()
    return any(mark in text for mark in (
        "429", "RESOURCE_EXHAUSTED", "RATE LIMIT", "RATE_LIMIT",
        "QUOTA", "TOO MANY REQUESTS"))


def _amount(response) -> float:
    """The engine records the proposed amount in its AUTHORITY check."""
    for check in response.checks:
        if check.check_id != "AUTHORITY":
            continue
        for ev in check.evidence:
            if ev.source == "computed" and ev.ref == "amount_usd":
                return round(float(ev.value), 2)
    return 0.0


def _run(case: dict) -> dict:
    started = time.perf_counter()
    response, error = None, None
    for attempt in range(RETRIES + 1):
        started = time.perf_counter()
        try:
            response = run_ticket(case)
            break
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the run
            if _daily_quota_exhausted(exc):
                raise SystemExit(
                    f"Daily quota exhausted; stopping evaluation: {exc}") from exc
            error = exc
            if not _rate_limited(exc) or attempt == RETRIES:
                break
            wait = RETRY_WAIT * (2 ** attempt)
            print(f"  rate limited; retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
    latency = time.perf_counter() - started

    if response is None:
        return {"error": f"{type(error).__name__}: {error}", "latency_s": latency,
                "model": None, "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "outcome": None,
                "human_approval_required": None, "approver_role": None,
                "amount_usd": None, "reason_codes": []}

    calls = response.trace.model_calls
    models = list(dict.fromkeys(call.model for call in calls))
    return {
        "outcome": response.outcome,
        "human_approval_required": response.human_approval_required,
        "approver_role": response.approver_role,
        "amount_usd": _amount(response),
        "reason_codes": response.reason_codes,
        "model": "+".join(models) if models else None,
        "input_tokens": sum(call.input_tokens for call in calls),
        "output_tokens": sum(call.output_tokens for call in calls),
        "cost_usd": response.trace.cost_usd,
        "latency_s": latency,
    }


def _check(expected: dict, actual: dict) -> list[str]:
    failures = []
    for field in ("outcome", "human_approval_required", "approver_role"):
        if actual.get(field) != expected.get(field):
            failures.append(
                f"{field}: expected {expected.get(field)!r}, got {actual.get(field)!r}")
    want, got = float(expected.get("amount_usd") or 0), float(actual.get("amount_usd") or 0)
    if abs(want - got) >= 0.005:
        failures.append(f"amount_usd: expected {want:.2f}, got {got:.2f}")
    missing = sorted(set(expected.get("reason_codes") or []) - set(actual["reason_codes"]))
    if missing:
        failures.append(f"missing reason codes: {', '.join(missing)}")
    if actual.get("error"):
        failures.append(actual["error"])
    return failures


def main() -> None:
    for name in ("GEMINI_API_KEY", "MODEL_NAME"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} must be set in the environment")

    cases = json.loads(CASES.read_text())
    rows = []
    for number, case in enumerate(cases, 1):
        print(f"[{number}/{len(cases)}] {case['ticket_id']}", flush=True)
        actual = _run(case)
        expected = {
            "outcome": case["expected_outcome"],
            "human_approval_required": case["expected_human_approval_required"],
            "approver_role": case["expected_approver_role"],
            "amount_usd": case["expected_amount_usd"],
            "reason_codes": case.get("expected_reason_codes") or [],
        }
        failures = _check(expected, actual)
        rows.append({
            "ticket_id": case["ticket_id"],
            "kind": case.get("kind", "seed"),
            "expected": expected,
            "actual": actual,
            "pass": not failures,
            "failures": failures,
        })
        print(f"  {'PASS' if not failures else 'FAIL'}: {actual['outcome']}"
              f" ${actual['amount_usd'] or 0:.2f} {actual['latency_s']:.1f}s", flush=True)
        if number < len(cases):
            time.sleep(DELAY)

    passed = sum(row["pass"] for row in rows)
    summary = {
        "cases": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "pass_rate": round(passed / len(rows), 4),
        "median_cost_usd": round(statistics.median(
            row["actual"]["cost_usd"] for row in rows), 6),
        "median_latency_s": round(statistics.median(
            row["actual"]["latency_s"] for row in rows), 3),
        "model_ids": sorted({row["actual"]["model"] for row in rows
                             if row["actual"]["model"]}),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{stamp}.json"
    path.write_text(json.dumps(
        {"run_at": stamp, "summary": summary, "cases": rows},
        indent=2, ensure_ascii=False) + "\n")
    print(f"\n{passed}/{len(rows)} passed ({summary['pass_rate']:.0%}) -> {path}")


if __name__ == "__main__":
    main()
