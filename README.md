# Larkfield support desk agent

A public HTTP service that reads one customer support email, extracts the facts,
applies refund policy RP-3.2 with a deterministic rules engine, and returns a
decision plus a draft reply. It proposes only — it never sends, refunds, or
changes anything — and pauses for human approval when policy requires it.

Live service: **https://larkfield-care-agent.onrender.com**

```bash
curl -s https://larkfield-care-agent.onrender.com/health
```

## Prepare the data

```bash
.venv/bin/python src/prepare_data.py
```

Reads `dataset/data/` (read-only, no LLM calls) and writes
`prepared/larkfield.db`. The build writes to a temporary file and replaces the
database only after all integrity checks pass, so it is safe to re-run. It
prints counts of rows loaded and issues fixed.

Tables: `customers`, `orders`, `order_items`, `shipments`, `refunds`,
`data_issues`, `build_meta`.

- Money is stored as integer cents (`*_cents`); divide by 100 for USD.
- Raw values are kept alongside parsed ones (`order_date` plus
  `order_date_raw`, and `order_total_raw`); `order_date_format` records whether
  the source was ISO, DD/MM or MM/DD.
- `customers.email_normalized` is the trimmed, lowercased matching key. Both
  CRM records per name are kept.
- `shipments.last_event_before_shipped` flags the 4 carrier rows whose last
  scan precedes the ship time; the carrier values themselves are unchanged.
- Every cleaning action is logged in `data_issues`; repeated `order_id`s with
  different content abort the build rather than silently picking one.

## Run the server

```bash
LARKFIELD_API_DB=prepared/api.db PORT=8000 .venv/bin/python -m src.api
```

Runs, decisions and the ledger are stored in the SQLite file named by
`LARKFIELD_API_DB` (default `prepared/api.db`); LangGraph checkpoints use the
same file. The port is read from `PORT` (default `8000`). The server listens on
`0.0.0.0`.

At startup the service checks the selected provider's credentials and fails
with a clear message if a required variable is missing. `GET /health` answers
without calling the model.

## Run the tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Run the eval

```bash
.venv/bin/python evals/run_eval.py
```

Set `PROVIDER`, `MODEL_NAME` and the matching provider API key first; a full
run costs a few cents. Runs all 28 labelled cases in `evals/cases.json` against the real model,
including the added negatives (wrong sender, other customer's order, prompt
injection, already refunded), and writes a timestamped report to
`evals/results/<timestamp>.json`.

**Pass rule:** a case passes only when its outcome, human-approval flag,
approver role, amount in USD and all required reason codes match the label.
The run summary reports cases, passed, failed, pass rate, median cost and
median latency.

## Environment variables

Names only — set the values in your environment or the Render dashboard.

| Variable | Purpose |
| --- | --- |
| `PROVIDER` | Model provider (`gemini` or `deepseek`) |
| `GEMINI_API_KEY` | Gemini credential |
| `DEEPSEEK_API_KEY` | DeepSeek credential |
| `MODEL_NAME` | Provider model name |
| `PORT` | HTTP port |
| `LARKFIELD_API_DB` | SQLite file for runs, decisions, ledger and checkpoints |
| `CHECKPOINT_DB` | Optional separate LangGraph checkpoint file |
| `MODEL_PRICE_INPUT` | Input token price used for cost accounting |
| `MODEL_PRICE_OUTPUT` | Output token price used for cost accounting |
| `EVAL_DELAY_SECONDS` | Pause between eval cases |
| `EVAL_MAX_RETRIES` | Retries on rate limits |
| `EVAL_RETRY_SECONDS` | Wait between retries |

## Endpoints

All examples target a local server at `http://localhost:8000`.

### `GET /health`

```bash
curl -s http://localhost:8000/health
```

### `POST /tickets`

```bash
curl -s -X POST http://localhost:8000/tickets -H 'Content-Type: application/json' \
  -d "{\"ticket_id\":\"T001\",\"received_at\":\"2026-10-01T09:00:00+00:00\",\"channel\":\"email\",\"from\":\"  ASHA.SHAH0@MAIL.EXAMPLE \",\"to\":\"support@larkfield.example\",\"subject\":\"Damaged Round wall mirror\",\"body\":\"Hi, my Round wall mirror from order LF-100200 arrived damaged (photo attached). I'd like a refund please. Thanks, Asha\",\"attachments\":[{\"file_name\":\"photo.jpg\",\"content_type\":\"image/jpeg\",\"content_base64\":\"\"}]}"
```

### `GET /runs/{run_id}`

```bash
curl -s http://localhost:8000/runs/RUN_ID
```

Returns the `TicketResponse` fields plus `status` (`PENDING`, `APPROVED`,
`REJECTED`, `COMPLETE`), `decision`, `approver` and `decided_at`.

### `POST /runs/{run_id}/decision`

```bash
curl -s -X POST http://localhost:8000/runs/RUN_ID/decision -H 'Content-Type: application/json' \
  -d '{"decision":"approve","approver":"support.lead@larkfield.example"}'
```

An approval is recorded once, however many decisions arrive.

### `GET /ledger`

```bash
curl -s http://localhost:8000/ledger
```

Returns one `LedgerEntry` per run that proposed an action (`REFUND`, `REPLY`
or `ESCALATION`), with status `PROPOSED`, `APPROVED` or `REJECTED`. Runs that
propose nothing have no row.

## Deploy

The image installs `requirements.txt`, builds `prepared/larkfield.db` from
`dataset/data/` during the build, runs as the non-root user `larkfield`, and
keeps its SQLite file at `LARKFIELD_API_DB` (image default `/app/data/api.db`).

```bash
docker build -t larkfield .
docker run --rm -p 8000:8000 \
  -e PROVIDER=gemini -e MODEL_NAME=<model> -e GEMINI_API_KEY=<key> larkfield
```

Render steps:

1. Push the repository and create a **New > Web Service** from it.
2. Choose the **Docker** runtime (the `Dockerfile` is detected automatically)
   and set the health check path to `/health`.
3. Add the environment variables in the Render dashboard, with no secrets
   committed to the repository:
   - `PROVIDER`: `gemini` (default) or `deepseek`
   - `MODEL_NAME`: the provider's model name
   - `GEMINI_API_KEY` when `PROVIDER=gemini`, or `DEEPSEEK_API_KEY` when
     `PROVIDER=deepseek`
   - `PORT` is set by Render; the container listens on `0.0.0.0`
   - `LARKFIELD_API_DB` only if you mount a persistent disk (point it at a
     writable path on that disk, e.g. `/app/data/api.db`)
4. Optional: attach a persistent disk at `/app/data` so runs, decisions and the
   ledger survive deploys. Without one, the container filesystem is ephemeral.

Startup fails with a clear message if the selected provider's key is missing.
`GET /health` answers without calling the model.

The live service runs with `PROVIDER=deepseek` and `MODEL_NAME=deepseek-flash`.
