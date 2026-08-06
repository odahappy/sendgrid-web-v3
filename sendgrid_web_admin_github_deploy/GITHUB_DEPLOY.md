# GitHub 一键部署说明

这个版本已经准备好放到 GitHub 后通过一条命令部署到 Ubuntu VPS。

## 1. 上传到 GitHub

先在 GitHub 创建一个空仓库，例如：

```text
sendgrid-web-admin
```

然后在本项目根目录执行：

```bash
git init
git add .
git commit -m "Initial VPS one-key deployment"
git branch -M main
git remote add origin https://github.com/你的GitHub用户名/sendgrid-web-admin.git
git push -u origin main
```

也可以直接用项目内的辅助脚本：

```bash
bash scripts/push_to_github.sh https://github.com/你的GitHub用户名/sendgrid-web-admin.git
```

## 2. VPS 一键安装应用和 HTTPS

在 Ubuntu VPS 上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/install_all.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 bash
```

请替换域名和证书通知邮箱。域名 A 记录必须指向 VPS，安全组需要开放 80 和 443。

安装器会自动处理 Ubuntu `unattended-upgrades` 占用 `apt/dpkg` 锁的情况：默认等待最多 900 秒，并在失败后修复 `dpkg`、最多重试 5 次。不要手工删除锁文件。

安装完成后访问：

```text
https://你的域名
```

后台初始账号和随机密码会显示在安装结果中。

## 3. 自定义安装参数

例如使用 9000 端口，并把 apt 锁等待时间改为 20 分钟：

```bash
curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/install_all.sh | sudo env DOMAIN=mailops.example.com EMAIL=admin@example.com APP_PORT=9000 APT_LOCK_TIMEOUT=1200 APT_RETRIES=5 bash
```

## 4. 常用运维命令

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

查看配置：

```bash
sudo cat /opt/sendgrid-web-admin/.env
```

备份数据库和上传文件：

```bash
sudo bash /opt/sendgrid-web-admin/scripts/backup_sqlite.sh
```

## 5. 注意

如果 VPS 云厂商有安全组、防火墙，需要放行对应端口，默认是 `8080`。
