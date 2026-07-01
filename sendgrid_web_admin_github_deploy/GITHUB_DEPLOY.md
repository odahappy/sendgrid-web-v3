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

## 2. VPS 一键安装

在 Ubuntu VPS 上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/你的GitHub用户名/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo env GITHUB_REPO=你的GitHub用户名/sendgrid-web-admin bash
```

安装完成后，脚本会自动输出访问地址：

```text
http://你的VPS公网IP:8080
```

后台初始账号：

```text
用户名：admin
密码：安装脚本输出的随机密码
```

## 3. 自定义端口

例如改成 9000：

```bash
curl -fsSL https://raw.githubusercontent.com/你的GitHub用户名/sendgrid-web-admin/main/scripts/onekey_install.sh | sudo env GITHUB_REPO=你的GitHub用户名/sendgrid-web-admin SERVER_PORT=9000 bash
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
