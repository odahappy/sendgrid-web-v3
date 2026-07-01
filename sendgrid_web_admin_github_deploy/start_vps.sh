#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.vps.example .env
  echo "Created .env from .env.vps.example. Please edit ADMIN_PASSWORD / SECRET_KEY / SERVICE_TOKEN before public use."
fi

mkdir -p data uploads/templates logs

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

set -a
# shellcheck disable=SC1091
source .env
set +a

HOST="${SERVER_HOST:-0.0.0.0}"
PORT="${SERVER_PORT:-8080}"

echo "Starting SendGrid Web Admin Scheduler on http://${HOST}:${PORT}"
exec uvicorn app.main:app --host "$HOST" --port "$PORT" --proxy-headers --forwarded-allow-ips='*'
