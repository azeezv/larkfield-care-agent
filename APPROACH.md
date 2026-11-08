# Approach

Work is split in two. The model ([src/extract.py](src/extract.py)) reads each
email and returns structured facts; the code ([src/policy.py](src/policy.py))
applies [RP-3.2](dataset/policies/refund_policy_RP-3.2.md) as named checks and
decides the outcome, amount and whether a person must approve. The model then
writes the draft reply from that decision and never chooses an outcome or
amount. [src/pipeline.py](src/pipeline.py) sequences the steps with LangGraph,
[src/api.py](src/api.py) serves the API, and
[src/prepare_data.py](src/prepare_data.py) builds the SQLite data.

RP-3.2 is small, so there is no RAG: policy is plain code with clause ids.
Email text is untrusted; instructions inside are ignored and the sender is
matched by address. A check that cannot finish is UNABLE_TO_VERIFY, never PASS.
An approval is recorded once, however many decisions arrive.

## Rules where RP-3.2 is silent

- Days are calendar dates in UTC (request date minus delivery or last-scan
  date), not elapsed hours; 30/45/10 are inclusive limits.
- Stalled days count from the last scan on record, even if it predates
  shipped_at; the evidence notes the inconsistency.
- An undelivered, still-moving claim gets no refund yet: outcome PROCEED with
  an empty reason-code list; the window check explains why.
- `affected_lines[].qty` is clamped to the ordered quantity (2.5), and a ticket
  asking for the whole order carries `requested_all` so 2.4 can reduce it.
- ORDER_STATUS and OTHER tickets never propose money.
- Requests outside RP-3.2's scope (for example, a goodwill voucher) return UNABLE_TO_VERIFY and propose no money.

## Evaluation

The eval has 28 cases; 26 pass; failures are described in ARTEFACT.md.
