#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
APP_PORT="${APP_PORT:-9000}"
GITHUB_REPO="${GITHUB_REPO:-odahappy/sendgrid-web-v3}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
PROJECT_SUBDIR="${PROJECT_SUBDIR:-sendgrid_web_admin_github_deploy}"
APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"

if [ -z "$DOMAIN" ]; then
  echo "ERROR: DOMAIN is required."
  echo "Example:"
  echo "curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/${PROJECT_SUBDIR}/scripts/install_all.sh | env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
  exit 1
fi

if [ -z "$EMAIL" ]; then
  echo "ERROR: EMAIL is required."
  echo "Example:"
  echo "curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/${PROJECT_SUBDIR}/scripts/install_all.sh | env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "Re-running as root with sudo..."
    exec sudo -E bash "$0"
  else
    echo "ERROR: Please run as root, or install sudo first."
    exit 1
  fi
fi

echo "============================================================"
echo "One-key install: SendGrid Web Admin + HTTPS"
echo "Domain: ${DOMAIN}"
echo "Email: ${EMAIL}"
echo "App port: ${APP_PORT}"
echo "Repo: ${GITHUB_REPO}"
echo "Branch: ${GITHUB_BRANCH}"
echo "Project dir: ${PROJECT_SUBDIR}"
echo "App dir: ${APP_DIR}"
echo "============================================================"

echo "Installing base packages..."
apt-get update
apt-get install -y sudo curl unzip ca-certificates

WORK_DIR="$(mktemp -d)"
SOURCE_ZIP="${WORK_DIR}/source.zip"
SOURCE_DIR="${WORK_DIR}/src"

echo "Downloading project from GitHub..."
curl -fL --connect-timeout 20 --retry 3 \
  -o "${SOURCE_ZIP}" \
  "https://github.com/${GITHUB_REPO}/archive/refs/heads/${GITHUB_BRANCH}.zip"

mkdir -p "${SOURCE_DIR}"
unzip -q "${SOURCE_ZIP}" -d "${SOURCE_DIR}"

REPO_NAME="$(basename "${GITHUB_REPO}")"
PROJECT_DIR="${SOURCE_DIR}/${REPO_NAME}-${GITHUB_BRANCH}/${PROJECT_SUBDIR}"

if [ ! -d "${PROJECT_DIR}" ]; then
  echo "ERROR: Project directory not found:"
  echo "${PROJECT_DIR}"
  echo ""
  echo "Please check PROJECT_SUBDIR=${PROJECT_SUBDIR}"
  exit 1
fi

cd "${PROJECT_DIR}"

echo "============================================================"
echo "Step 1/2: Installing application..."
echo "============================================================"

SERVER_PORT="${APP_PORT}" APP_DIR="${APP_DIR}" bash scripts/install_ubuntu_vps.sh

echo "Checking application service..."
systemctl restart sendgrid-web-admin || true
sleep 2

if curl -fsS --max-time 10 "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; then
  APP_HEALTH_STATUS="OK"
else
  APP_HEALTH_STATUS="CHECK_SERVICE_LOG"
fi

echo "============================================================"
echo "Step 2/2: Setting up HTTPS..."
echo "============================================================"

DOMAIN="${DOMAIN}" \
EMAIL="${EMAIL}" \
APP_PORT="${APP_PORT}" \
APP_DIR="${APP_DIR}" \
bash scripts/setup_https.sh

ADMIN_PASSWORD_PRINT=""
if [ -f "${APP_DIR}/.env" ]; then
  ADMIN_PASSWORD_PRINT="$(grep '^ADMIN_PASSWORD=' "${APP_DIR}/.env" | cut -d= -f2- || true)"
fi

if [ -z "$ADMIN_PASSWORD_PRINT" ]; then
  ADMIN_PASSWORD_PRINT="未找到，请执行：grep '^ADMIN_PASSWORD=' ${APP_DIR}/.env"
fi

echo ""
echo "============================================================"
echo "All installed successfully."
echo "Access URL: https://${DOMAIN}"
echo "ADMIN_USERNAME=admin"
echo "ADMIN_PASSWORD=${ADMIN_PASSWORD_PRINT}"
echo ""
echo "App health: ${APP_HEALTH_STATUS}"
echo "App proxy: http://127.0.0.1:${APP_PORT}"
echo "Service: sendgrid-web-admin"
echo "Check app logs: journalctl -u sendgrid-web-admin -f"
echo "Nginx config: /etc/nginx/sites-available/sendgrid-web-admin"
echo "Config file: ${APP_DIR}/.env"
echo "============================================================"
