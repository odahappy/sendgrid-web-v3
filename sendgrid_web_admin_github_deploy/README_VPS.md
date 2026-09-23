# GitHub 一键部署版

一键部署命令格式：

```bash
curl -fsSL https://raw.githubusercontent.com/你的GitHub用户名/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo env GITHUB_REPO=你的GitHub用户名/sendgrid-web-admin bash
```

安装脚本默认只监听 `127.0.0.1`，避免后台端口直接暴露。推荐使用 `scripts/install_all.sh` 一次完成应用和 HTTPS 安装。

详细说明见 `GITHUB_DEPLOY.md` 和 `OPTIMIZATION_NOTES.md`。


## GitHub 一条命令安装应用和 HTTPS

在 Ubuntu VPS 上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/install_all.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash
```

安装器会自动等待 Ubuntu 的 `apt/dpkg` 锁，默认最多等待 900 秒，并在软件包安装失败时自动修复未完成的 `dpkg` 配置后重试 5 次。不要手工删除 `/var/lib/dpkg/lock-frontend`。

可选参数：

```text
APT_LOCK_TIMEOUT=900   # 等待 apt 锁的最长秒数
APT_RETRIES=5          # apt-get 最大尝试次数
APT_RETRY_DELAY=10     # 每次重试前等待秒数
```

重复运行安装命令会保留 `/opt/sendgrid-web-admin/.env`、`data/`、`uploads/`、`logs/` 和 `backups/`。同步新代码前，安装器会把现有 `.env` 与 SQLite 数据库的一致性快照保存到 `backups/pre-install-日期时间-随机字符/`；若备份失败，安装立即停止。自定义 `DATABASE_PATH` 也会受到同步保护。快照里的 `.env`、`database.db` 含敏感数据，应限制访问并定期清理不再需要的旧快照。

---

# SendGrid Web Admin Scheduler - VPS 运行版

这个版本已经加入 VPS 运行文件，支持两种部署方式：

1. **普通 Ubuntu VPS + systemd**：推荐，适合长期运行。
2. **Docker / docker-compose**：适合会用 Docker 的服务器。

> 重要：本项目内部有后台发送线程，所以不要用多个 uvicorn/gunicorn worker。多个 worker 会导致多个后台线程同时发送，可能重复发信。本 VPS 版默认只启动 1 个进程。

---

## 一、Ubuntu VPS 一键安装，推荐

假设你已经把整个项目目录上传到 VPS，例如：

```bash
cd sendgrid_web_admin_vps
bash scripts/install_ubuntu_vps.sh
```

安装脚本会自动完成：

- 安装 Python3、venv、pip、rsync、curl
- 复制项目到 `/opt/sendgrid-web-admin`
- 创建 `.venv`
- 安装 requirements
- 自动生成 `.env`
- 自动生成随机后台密码、独立的 SECRET_KEY、DATA_ENCRYPTION_KEY、SERVICE_TOKEN
- 创建 systemd 服务
- 开机自启
- 启动服务

安装完成后会显示随机管理员密码。默认后台只监听本机，请继续运行 HTTPS 安装脚本，或使用 `scripts/install_all.sh`。

确实需要临时公开应用端口时，显式设置 `SERVER_HOST=0.0.0.0 EXPOSE_APP_PORT=true`；不建议长期这样运行。

---

## 二、常用命令

查看运行状态：

```bash
sudo systemctl status sendgrid-web-admin
```

查看实时日志：

```bash
sudo journalctl -u sendgrid-web-admin -f
```

重启服务：

```bash
sudo systemctl restart sendgrid-web-admin
```

停止服务：

```bash
sudo systemctl stop sendgrid-web-admin
```

修改配置：

```bash
sudo nano /opt/sendgrid-web-admin/.env
sudo systemctl restart sendgrid-web-admin
```

备份数据库和上传文件：

```bash
cd /opt/sendgrid-web-admin
bash scripts/backup_sqlite.sh
```

检查健康状态：

```bash
cd /opt/sendgrid-web-admin
bash scripts/check_status.sh
```

---

## 升级与回滚

升级前留存上一版本的项目源码，确认磁盘有足够空间容纳 SQLite 快照。重新执行安装命令后，记下输出中的 `Pre-install snapshot` 路径。已有的上传文件和日志会保留在原目录，但不会复制进这个升级前快照；若也需要独立备份上传文件，可提前运行 `bash scripts/backup_sqlite.sh`。

回滚会把数据库恢复到升级前的状态，期间新增的任务、收件人和发送记录会丢失。停服务后，用上一版本源码覆盖程序文件，再恢复升级前的配置和数据库：

```bash
sudo systemctl stop sendgrid-web-admin
APP_DIR=/opt/sendgrid-web-admin
OLD_SRC=/path/to/previous/release/sendgrid_web_admin_github_deploy
SNAP=/opt/sendgrid-web-admin/backups/pre-install-替换为实际快照目录名

sudo rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude 'uploads' --exclude 'logs' \
  --exclude '/.env' --exclude '/backups' \
  "$OLD_SRC/" "$APP_DIR/"
SERVICE_USER="$(systemctl show -p User --value sendgrid-web-admin)"
SERVICE_USER="${SERVICE_USER:-root}"
SERVICE_GROUP="$(systemctl show -p Group --value sendgrid-web-admin)"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
sudo install -m 600 "$SNAP/.env" "$APP_DIR/.env"
if [ -f "$SNAP/database.db" ]; then
  DB_PATH="$(sudo cat "$SNAP/database-path.txt")"
  sudo mkdir -p "$(dirname "$DB_PATH")"
  sudo rm -f "$DB_PATH-wal" "$DB_PATH-shm"
  sudo cp "$SNAP/database.db" "$DB_PATH"
  sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$DB_PATH"
  sudo chmod 600 "$DB_PATH"
fi
sudo "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
sudo chmod 600 "$APP_DIR/.env"
sudo systemctl start sendgrid-web-admin
sudo systemctl status sendgrid-web-admin --no-pager
```

如果快照里没有 `database.db` 与 `database-path.txt`，代表升级前尚未生成数据库，示例会跳过数据库恢复。上述操作假定安装目录和服务名保持默认值；自定义配置时使用实际路径和服务名。

---

## 三、手动运行方式

如果你不想装 systemd，可以直接运行：

```bash
cd sendgrid_web_admin_vps
bash start_vps.sh
```

第一次运行会自动创建 `.env`，但你需要手动修改：

```bash
nano .env
```

至少要修改：

```env
ADMIN_PASSWORD=你的后台密码
SECRET_KEY=一串很长的随机字符串
SERVICE_TOKEN=一串很长的随机字符串
```

---

## 四、Docker 运行方式

先复制配置：

```bash
cp .env.vps.example .env
nano .env
```

修改密码和密钥后运行：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

数据会保存在宿主机：

```text
data/
uploads/
logs/
```

---

## 五、Nginx 反向代理，可选

如果你想用域名访问，例如：

```text
http://mail.example.com
```

可以使用：

```bash
sudo apt-get install -y nginx
sudo cp deploy/nginx/sendgrid-web-admin.conf /etc/nginx/sites-available/sendgrid-web-admin
sudo ln -s /etc/nginx/sites-available/sendgrid-web-admin /etc/nginx/sites-enabled/sendgrid-web-admin
sudo nginx -t
sudo systemctl reload nginx
```

然后把 `.env` 改为只监听本机：

```env
SERVER_HOST=127.0.0.1
SERVER_PORT=8080
SERVER_AUTO_INCREMENT_PORT=false
```

重启：

```bash
sudo systemctl restart sendgrid-web-admin
```

Nginx 配置里已经设置：

```nginx
client_max_body_size 50m;
```

该上限覆盖默认多文件上传场景，同时避免无限请求体耗尽内存。

---

## 六、防火墙端口

如果你直接使用 `IP:8080` 访问，需要 VPS 防火墙和云厂商安全组放行 8080。

Ubuntu ufw 示例：

```bash
sudo ufw allow 8080/tcp
sudo ufw reload
```

如果使用 Nginx 80 端口：

```bash
sudo ufw allow 80/tcp
sudo ufw reload
```

---

## 七、目录说明

```text
/opt/sendgrid-web-admin
├── app/                    主程序
├── data/                   SQLite 数据库
├── uploads/                HTML 模板、上传文件
├── logs/                   预留日志目录
├── scripts/                VPS 运维脚本
├── deploy/nginx/           Nginx 示例配置
├── deploy/systemd/         systemd 服务模板
├── .env                    VPS 配置文件
├── Dockerfile
└── docker-compose.yml
```

---

## 八、重要注意事项

1. **不要启动多个 worker**。本项目后台发送线程在应用进程内运行，多进程会有重复发送风险。
2. **不要随意修改 SECRET_KEY**。它用于保护数据库里的 API Key 和代理地址，修改后旧数据可能无法正确解密。
3. **收件人池上传有安全限制**。通过 `MAX_RECIPIENT_UPLOAD_BYTES` 和 `MAX_RECIPIENT_FILES_PER_UPLOAD` 调整；Nginx 示例上限为 50 MB。
4. **SECRET_KEY 与 DATA_ENCRYPTION_KEY 必须分离**。前者签名 Session，后者使用 AES-GCM 保护 API Key 和代理密码。
5. **SQLite 适合单机 VPS**。计划生成和容量预留已做原子事务，但仍建议保持单应用进程。
