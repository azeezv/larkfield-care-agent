# Artefact

## Data preparation

[src/prepare_data.py](src/prepare_data.py) builds `prepared/larkfield.db` from
`dataset/data/` using only the standard library, with no LLM calls. It writes to
a temporary file and replaces the database only after every integrity check
passes, so re-running is safe.

- Rows kept: 53 customers, 47 orders, 57 order items, 47 shipments and 3
  refunds. The 49 source orders became 47 after 2 identical duplicate rows were
  removed (`LF-100216`, `LF-100239`).
- All 53 customer rows were kept (30 names; 23 names have two CRM records),
  with a trimmed, lowercased `email_normalized` matching key.
- 47 order totals were parsed to integer cents: 16 `$`, 15 `USD` and 16 plain
  amounts.
- 47 order dates were parsed: 13 slash-format and 3 ambiguous. The ambiguous
  dates were resolved against `shipped_at`: 1 had only one reading before
  shipment; 2 used the latest reading before shipment (tie-break).
- 4 shipments whose last carrier scan precedes `shipped_at` were flagged, with
  carrier values unchanged.
- Every change was logged in `data_issues`; all 15 integrity checks passed.

## Policy

We used [RP-3.2](dataset/policies/refund_policy_RP-3.2.md) (version 3.2,
effective 2026-07-01). It states that it replaces RP-3.1 and all earlier
customer-care guidance on refunds and returns, so it supersedes the older
[HC-FAQ v2](dataset/policies/help_centre_faq_v2.md) (published 2025-10-14). The
FAQ also conflicts with current policy: it says 14 days (RP-3.2: 30, or 45 with
a photo), automatic refunds under $250 (RP-3.2: up to $150 without review) and
contact us after 5 days without tracking (RP-3.2: more than 10 days without
movement is lost in transit).

## Eval pass rule

[evals/run_eval.py](evals/run_eval.py) passes a case only when `outcome`,
`human_approval_required` and `approver_role` match exactly, the amount is
within $0.005 of the expected value, every expected reason code is present
(extra codes are allowed), and the run raised no error.

## Before and after

| Run | Cases | Passed | Failed | Pass rate | Median cost (USD) | Median latency (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| First, 20261108T052037Z | 21 | 21 | 0 | 1.0 | 0.000464 | 2.317 |
| Before, 20261108T052855Z | 28 | 25 | 3 | 0.8929 | 0.00048 | 2.316 |
| After, 20261108T053604Z | 28 | 26 | 2 | 0.9286 | 0.000473 | 2.284 |

The first run was the 21-case set
([20261108T052037Z.json](evals/results/20261108T052037Z.json)). The only change
between the before and after runs was matching the customer's wording to an
order line by comparing words; an ambiguous or missing match still returns
`UNABLE_TO_VERIFY`.

The change fixed `H006` (`amount_and_wording`), which moved from
`UNABLE_TO_VERIFY` at $0.00 to `PROCEED` at $45.00. `H005` still fails on a
missing `CUSTOMER_MISMATCH` reason code, and `H007` still returns
`UNABLE_TO_VERIFY` where `PROCEED` is expected.

## Cost and latency

- Cost: each case's `trace.cost_usd` comes from its token counts times the
  `deepseek-flash` price table, and the reported figure is the
  `statistics.median` across the run.
- Latency: each call to `run_ticket` is timed with `time.perf_counter()`, and
  the reported figure is the `statistics.median` of those per-case times.
- Medians in the 28-case files: $0.00048 at 2.316 s (before) and $0.000473 at
  2.284 s (after). Prices are DeepSeek's peak `deepseek-flash` rates, checked on
  8 November 2026, so the real cost is lower off-peak.

## What is not built

- **H005 (two orders):** the outcome is right (`INVALID_REQUEST`) but the reason
  code is `ORDER_NOT_FOUND` instead of `CUSTOMER_MISMATCH`; the extractor
  returns no order reference when more than one distinct order number appears.
- **H007 (goodwill voucher):** RP-3.2 does not cover vouchers, and the expected
  `PROCEED` result is our own interpretation; the engine returns
  `UNABLE_TO_VERIFY`.
- **Render free tier:** the disk is ephemeral and the service sleeps when idle,
  so runs and the ledger can be lost and the first request after a pause is slow.
- **No RAG:** the policy is small and implemented as code with clause ids, so
  retrieval is unnecessary.
- **No queue:** model calls run inside the request and approvals wait in SQLite,
  with no worker queue.
- **No PII redaction** before the model call or in logs, and **no separate
  injection classifier**; injection detection is a field in the extraction
  prompt.
- **No email-size limit** and **no attachment-content inspection**; attachments
  are only checked for an image type or extension.
- **No rate limiting, endpoint authentication, monitoring, or more than one
  instance.**
- **SQLite, not Postgres:** runs, checkpoints and the ledger share SQLite files;
  production would use Postgres.
- **Small eval:** 28 cases with labels we wrote ourselves.
