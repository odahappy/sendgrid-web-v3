#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
APP_PORT="${APP_PORT:-9000}"
APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
NGINX_CONF_NAME="${NGINX_CONF_NAME:-sendgrid-web-admin}"

if [ -z "$DOMAIN" ]; then
  echo "ERROR: DOMAIN is required."
  echo "Example:"
  echo "curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/setup_https.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
  exit 1
fi

if [ -z "$EMAIL" ]; then
  echo "ERROR: EMAIL is required."
  echo "Example:"
  echo "curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/setup_https.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash"
  exit 1
fi

echo "============================================================"
echo "Setting up HTTPS for SendGrid Web Admin"
echo "Domain: ${DOMAIN}"
echo "Email: ${EMAIL}"
echo "App port: ${APP_PORT}"
echo "App dir: ${APP_DIR}"
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
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx curl ca-certificates

echo "Checking local app service..."
if curl -fsS --max-time 5 "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; then
  echo "App health check: OK"
else
  echo "WARNING: Cannot access http://127.0.0.1:${APP_PORT}/api/health"
  echo "继续配置 Nginx 和 HTTPS，但请确认项目已经运行在 ${APP_PORT} 端口。"
  echo "如果后续访问域名出现 502，请执行：sudo systemctl restart sendgrid-web-admin"
fi

echo "Creating Nginx reverse proxy config..."

cat > "/etc/nginx/sites-available/${NGINX_CONF_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 0;

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
EOF

mkdir -p /var/www/html
ln -sf "/etc/nginx/sites-available/${NGINX_CONF_NAME}" "/etc/nginx/sites-enabled/${NGINX_CONF_NAME}"
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable nginx
systemctl restart nginx

if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
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

echo "Testing certificate auto-renew..."
certbot renew --dry-run

ADMIN_PASSWORD_PRINT=""
if [ -f "${APP_DIR}/.env" ]; then
  ADMIN_PASSWORD_PRINT="$(grep '^ADMIN_PASSWORD=' "${APP_DIR}/.env" | cut -d= -f2- || true)"
fi

if [ -z "$ADMIN_PASSWORD_PRINT" ]; then
  ADMIN_PASSWORD_PRINT="未找到，请执行：sudo grep '^ADMIN_PASSWORD=' ${APP_DIR}/.env"
fi

echo ""
echo "============================================================"
echo "HTTPS installed successfully."
echo "Access URL: https://${DOMAIN}"
echo "ADMIN_PASSWORD=${ADMIN_PASSWORD_PRINT}"
echo ""
echo "App proxy: http://127.0.0.1:${APP_PORT}"
echo "Nginx config: /etc/nginx/sites-available/${NGINX_CONF_NAME}"
echo "Certbot renew test: OK"
echo "============================================================"
