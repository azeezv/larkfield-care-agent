FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY dataset ./dataset

# Build the read-only prepared database from the raw data. No LLM calls.
RUN python src/prepare_data.py

# Runs, decisions, ledger and checkpoints live in a writable folder.
RUN useradd --create-home --uid 10001 larkfield \
    && mkdir -p /app/data \
    && chown -R larkfield:larkfield /app/data
USER larkfield

ENV LARKFIELD_API_DB=/app/data/api.db

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn src.api:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
