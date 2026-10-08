#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python -m app
