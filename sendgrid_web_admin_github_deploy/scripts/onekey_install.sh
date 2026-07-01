#!/usr/bin/env bash
set -euo pipefail

# GitHub 一键部署入口。
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/scripts/onekey_install.sh | sudo GITHUB_REPO=OWNER/REPO bash
# 可选环境变量：
#   GITHUB_REPO=OWNER/REPO
#   GITHUB_BRANCH=main
#   APP_DIR=/opt/sendgrid-web-admin
#   SERVER_PORT=8080
#   SERVICE_NAME=sendgrid-web-admin

GITHUB_REPO="${GITHUB_REPO:-}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
SERVER_PORT="${SERVER_PORT:-8080}"
SERVICE_NAME="${SERVICE_NAME:-sendgrid-web-admin}"

if [ -z "${GITHUB_REPO}" ]; then
  cat <<'MSG'
ERROR: GITHUB_REPO is required.

Example:
  curl -fsSL https://raw.githubusercontent.com/YOUR_NAME/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo GITHUB_REPO=YOUR_NAME/sendgrid-web-admin bash
MSG
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required. Please run on Ubuntu/Debian VPS."
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Ubuntu/Debian VPS only."
  exit 1
fi

export APP_DIR SERVER_PORT SERVICE_NAME

echo "Installing download tools..."
sudo apt-get update
sudo apt-get install -y curl unzip ca-certificates

WORK_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

ZIP_URL="https://github.com/${GITHUB_REPO}/archive/refs/heads/${GITHUB_BRANCH}.zip"
echo "Downloading project: ${ZIP_URL}"
curl -fL --connect-timeout 15 --retry 3 -o "$WORK_DIR/source.zip" "$ZIP_URL"

unzip -q "$WORK_DIR/source.zip" -d "$WORK_DIR/src"
SRC_DIR="$(find "$WORK_DIR/src" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$SRC_DIR" ] || [ ! -d "$SRC_DIR" ]; then
  echo "Failed to find extracted source directory."
  exit 1
fi

if [ ! -f "$SRC_DIR/scripts/install_ubuntu_vps.sh" ]; then
  echo "Missing scripts/install_ubuntu_vps.sh in repository."
  exit 1
fi

bash "$SRC_DIR/scripts/install_ubuntu_vps.sh"
