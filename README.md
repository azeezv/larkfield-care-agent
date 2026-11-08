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

