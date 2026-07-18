#!/usr/bin/env bash
# Boot the Securities Valuation Engine locally.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

[ -f .env ] || cp .env.example .env   # create empty key file on first run
exec ./.venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --reload
