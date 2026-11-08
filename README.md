# Larkfield support desk agent

## Prepare the data

```bash
.venv/bin/python src/prepare_data.py
```

Reads `dataset/data/` (read-only, no LLM calls) and writes
`prepared/larkfield.db`. The build writes to a temporary file and replaces the
database only after all integrity checks pass, so it is safe to re-run.

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

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Run the API

```bash
LARKFIELD_API_DB=prepared/api.db PORT=8000 .venv/bin/python -m src.api
```

Runs, decisions and the ledger are stored in the SQLite file named by
`LARKFIELD_API_DB` (default `prepared/api.db`); LangGraph checkpoints use the
same file. The port is read from `PORT` (default `8000`).

`GET /runs/{run_id}` and `POST /runs/{run_id}/decision` return the
`TicketResponse` fields plus:

- `status`: `PENDING`, `APPROVED`, `REJECTED` or `COMPLETE`.
- `decision`: `approve`, `reject` or null.
- `approver` and `decided_at`: the human decision details, or null.

`GET /ledger` returns `LedgerEntry` rows for approved actions only.

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
