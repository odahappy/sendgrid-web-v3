#!/usr/bin/env bash
set -euo pipefail

REMOTE_URL="${1:-}"
if [ -z "$REMOTE_URL" ]; then
  cat <<'MSG'
Usage:
  bash scripts/push_to_github.sh https://github.com/YOUR_NAME/sendgrid-web-admin.git
or:
  bash scripts/push_to_github.sh git@github.com:YOUR_NAME/sendgrid-web-admin.git

Before running, create an empty GitHub repository first.
MSG
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Please install Git first."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d .git ]; then
  git init
fi

git branch -M main
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi

git add .
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Initial VPS one-key deployment"
fi

git push -u origin main

echo ""
echo "Pushed to GitHub: $REMOTE_URL"
echo "VPS one-key command example:"
echo "curl -fsSL https://raw.githubusercontent.com/YOUR_NAME/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo env GITHUB_REPO=YOUR_NAME/sendgrid-web-admin bash"
