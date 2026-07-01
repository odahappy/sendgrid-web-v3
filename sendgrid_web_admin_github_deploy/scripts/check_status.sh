#!/usr/bin/env bash
set -euo pipefail
SERVICE_NAME="${SERVICE_NAME:-sendgrid-web-admin}"
PORT="${SERVER_PORT:-8080}"

echo "Systemd status:"
sudo systemctl --no-pager --full status "$SERVICE_NAME" || true

echo ""
echo "Health check:"
curl -fsS "http://127.0.0.1:${PORT}/api/health" || true

echo ""
echo "Recent logs:"
sudo journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
