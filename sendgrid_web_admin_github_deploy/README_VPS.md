# GitHub 一键部署版

一键部署命令格式：

```bash
curl -fsSL https://raw.githubusercontent.com/你的GitHub用户名/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo env GITHUB_REPO=你的GitHub用户名/sendgrid-web-admin bash
```

安装完成后，终端会直接输出访问地址，例如：

```text
http://你的VPS公网IP:8080
```

详细说明见 `GITHUB_DEPLOY.md`。

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
- 自动生成随机后台密码、SECRET_KEY、SERVICE_TOKEN
- 创建 systemd 服务
- 开机自启
- 启动服务

安装完成后会显示：

```text
Initial admin login: admin / 随机密码
Access URL: http://YOUR_VPS_IP:8080
```

访问：

```text
http://你的VPS公网IP:8080
```

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
client_max_body_size 0;
```

所以收件人池/库上传不会被 Nginx 文件大小限制拦住。

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
3. **收件人池上传已解除应用层大小限制**。如果走 Nginx，也要保证 `client_max_body_size 0`。
4. **SQLite 适合单机 VPS**。如果后续多台服务器同时运行，建议再升级数据库和任务锁机制。
