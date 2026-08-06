#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
APP_PORT="${APP_PORT:-9000}"
APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
NGINX_CONF_NAME="${NGINX_CONF_NAME:-sendgrid-web-admin}"
SERVICE_NAME="${SERVICE_NAME:-sendgrid-web-admin}"
APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT:-900}"
APT_RETRIES="${APT_RETRIES:-5}"
APT_RETRY_DELAY="${APT_RETRY_DELAY:-10}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: This script must run as root."
  echo "Example:"
  echo "curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/setup_https.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
  exit 1
fi

if [ -z "$DOMAIN" ]; then
  echo "ERROR: DOMAIN is required."
  exit 1
fi

if [ -z "$EMAIL" ]; then
  echo "ERROR: EMAIL is required."
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
echo "Setting up HTTPS for SendGrid Web Admin"
echo "Domain: ${DOMAIN}"
echo "Email: ${EMAIL}"
echo "App port: ${APP_PORT}"
echo "App dir: ${APP_DIR}"
echo "Apt lock timeout: ${APT_LOCK_TIMEOUT}s"
echo "============================================================"

echo "Checking port usage..."

if ss -tulpn | grep -E ":80 " | grep -v nginx >/dev/null 2>&1; then
  echo "ERROR: 80端口已被非 Nginx 程序占用。"
  echo "请先处理 80 端口占用，否则 Let's Encrypt 无法正常验证域名。"
  ss -tulpn | grep -E ":80 "
  exit 1
fi

if ss -tulpn | grep -E ":443 " | grep -v nginx >/dev/null 2>&1; then
  echo "ERROR: 443端口已被非 Nginx 程序占用。"
  echo "请先处理 443 端口占用，否则 HTTPS 无法正常监听。"
  ss -tulpn | grep -E ":443 "
  exit 1
fi

echo "Installing Nginx and Certbot..."
apt_retry update
apt_retry install -y nginx certbot python3-certbot-nginx curl ca-certificates psmisc

echo "Checking local app service..."
if curl -fsS --max-time 5 "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; then
  echo "App health check: OK"
else
  echo "WARNING: Cannot access http://127.0.0.1:${APP_PORT}/api/health"
  echo "继续配置 Nginx 和 HTTPS，但请确认项目已经运行在 ${APP_PORT} 端口。"
  echo "如果后续访问域名出现 502，请执行：systemctl restart sendgrid-web-admin"
fi

echo "Creating Nginx reverse proxy config..."

mkdir -p /var/www/html

cat > "/etc/nginx/sites-available/${NGINX_CONF_NAME}" <<EOF_NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 50m;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
        try_files \$uri =404;
    }

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_connect_timeout 300;
        proxy_send_timeout 300;
        proxy_read_timeout 300;
    }
}
EOF_NGINX

ln -sf "/etc/nginx/sites-available/${NGINX_CONF_NAME}" "/etc/nginx/sites-enabled/${NGINX_CONF_NAME}"
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable nginx
systemctl restart nginx

if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  ufw delete allow "${APP_PORT}/tcp" >/dev/null 2>&1 || true
fi

# Ensure the backend cannot be reached directly after HTTPS is enabled.
if [ -f "${APP_DIR}/.env" ]; then
  sed -i 's/^SERVER_HOST=.*/SERVER_HOST=127.0.0.1/' "${APP_DIR}/.env"
  sed -i 's/^SESSION_COOKIE_SECURE=.*/SESSION_COOKIE_SECURE=true/' "${APP_DIR}/.env"
fi
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ -f "${SERVICE_FILE}" ]; then
  sed -i -E 's/--host [^ ]+/--host 127.0.0.1/' "${SERVICE_FILE}"
  systemctl daemon-reload
  systemctl restart "${SERVICE_NAME}"
fi

echo "Testing HTTP access..."
curl -I --max-time 10 "http://${DOMAIN}" || true

echo "Requesting HTTPS certificate..."

certbot --nginx \
  -d "${DOMAIN}" \
  --redirect \
  --agree-tos \
  -m "${EMAIL}" \
  --no-eff-email \
  --non-interactive

ADMIN_PASSWORD_PRINT=""
if [ -f "${APP_DIR}/.env" ]; then
  ADMIN_PASSWORD_PRINT="$(grep '^ADMIN_PASSWORD=' "${APP_DIR}/.env" | cut -d= -f2- || true)"
fi

if [ -z "$ADMIN_PASSWORD_PRINT" ]; then
  ADMIN_PASSWORD_PRINT="未找到，请执行：grep '^ADMIN_PASSWORD=' ${APP_DIR}/.env"
fi

echo ""
echo "============================================================"
echo "HTTPS installed successfully."
echo "Access URL: https://${DOMAIN}"
echo "ADMIN_USERNAME=admin"
echo "ADMIN_PASSWORD=${ADMIN_PASSWORD_PRINT}"
echo ""
echo "App proxy: http://127.0.0.1:${APP_PORT}"
echo "Nginx config: /etc/nginx/sites-available/${NGINX_CONF_NAME}"
echo "Certbot auto renew: enabled"
echo "Config file: ${APP_DIR}/.env"
echo "============================================================"
