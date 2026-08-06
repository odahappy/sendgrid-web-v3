#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
APP_PORT="${APP_PORT:-9000}"
GITHUB_REPO="${GITHUB_REPO:-odahappy/sendgrid-web-v3}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
PROJECT_SUBDIR="${PROJECT_SUBDIR:-sendgrid_web_admin_github_deploy}"
APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT:-900}"
APT_RETRIES="${APT_RETRIES:-5}"
APT_RETRY_DELAY="${APT_RETRY_DELAY:-10}"

print_install_example() {
  echo "curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/${PROJECT_SUBDIR}/scripts/install_all.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
}

if [ -z "$DOMAIN" ]; then
  echo "ERROR: DOMAIN is required."
  echo "Example:"
  print_install_example
  exit 1
fi

if [ -z "$EMAIL" ]; then
  echo "ERROR: EMAIL is required."
  echo "Example:"
  print_install_example
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: This installer must run as root."
  echo "Run it with sudo as shown below:"
  print_install_example
  exit 1
fi

apt_is_busy() {
  local lock_file

  if command -v fuser >/dev/null 2>&1; then
    for lock_file in \
      /var/lib/dpkg/lock-frontend \
      /var/lib/dpkg/lock \
      /var/cache/apt/archives/lock \
      /var/lib/apt/lists/lock; do
      if [ -e "$lock_file" ] && fuser "$lock_file" >/dev/null 2>&1; then
        return 0
      fi
    done
  fi

  pgrep -x apt >/dev/null 2>&1 \
    || pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x dpkg >/dev/null 2>&1 \
    || pgrep -f '/usr/bin/unattended-upgrade' >/dev/null 2>&1 \
    || pgrep -f 'unattended-upgr' >/dev/null 2>&1
}

wait_for_apt() {
  local started_at now elapsed
  started_at="$(date +%s)"

  while apt_is_busy; do
    now="$(date +%s)"
    elapsed=$((now - started_at))

    if [ "$elapsed" -ge "$APT_LOCK_TIMEOUT" ]; then
      echo "ERROR: apt/dpkg remained busy for ${APT_LOCK_TIMEOUT} seconds."
      echo "Active package processes:"
      ps -ef | grep -E '[a]pt|[d]pkg|[u]nattended' || true
      return 1
    fi

    echo "apt/dpkg is busy, waiting... (${elapsed}s/${APT_LOCK_TIMEOUT}s)"
    sleep 5
  done
}

repair_dpkg() {
  wait_for_apt
  DEBIAN_FRONTEND=noninteractive dpkg --configure -a || true
}

apt_retry() {
  local attempt=1
  local rc=0

  while [ "$attempt" -le "$APT_RETRIES" ]; do
    wait_for_apt
    echo "Running apt-get $* (attempt ${attempt}/${APT_RETRIES})..."

    if DEBIAN_FRONTEND=noninteractive apt-get \
      -o DPkg::Lock::Timeout=120 \
      -o Acquire::Retries=3 \
      "$@"; then
      return 0
    else
      rc=$?
    fi

    echo "WARNING: apt-get failed with exit code ${rc}."
    repair_dpkg

    if [ "$attempt" -lt "$APT_RETRIES" ]; then
      echo "Retrying in ${APT_RETRY_DELAY} seconds..."
      sleep "$APT_RETRY_DELAY"
    fi

    attempt=$((attempt + 1))
  done

  echo "ERROR: apt-get $* failed after ${APT_RETRIES} attempts."
  return "$rc"
}

echo "============================================================"
echo "One-key install: SendGrid Web Admin + HTTPS"
echo "Domain: ${DOMAIN}"
echo "Email: ${EMAIL}"
echo "App port: ${APP_PORT}"
echo "Repo: ${GITHUB_REPO}"
echo "Branch: ${GITHUB_BRANCH}"
echo "Project dir: ${PROJECT_SUBDIR}"
echo "App dir: ${APP_DIR}"
echo "Apt lock timeout: ${APT_LOCK_TIMEOUT}s"
echo "============================================================"

echo "Installing base packages..."
apt_retry update
apt_retry install -y sudo curl unzip ca-certificates psmisc

WORK_DIR="$(mktemp -d)"
SOURCE_ZIP="${WORK_DIR}/source.zip"
SOURCE_DIR="${WORK_DIR}/src"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Downloading project from GitHub..."
curl -fL --connect-timeout 20 --retry 5 --retry-delay 3 --retry-all-errors \
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

SERVER_PORT="${APP_PORT}" \
APP_DIR="${APP_DIR}" \
APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT}" \
APT_RETRIES="${APT_RETRIES}" \
bash scripts/install_ubuntu_vps.sh

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
APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT}" \
APT_RETRIES="${APT_RETRIES}" \
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
