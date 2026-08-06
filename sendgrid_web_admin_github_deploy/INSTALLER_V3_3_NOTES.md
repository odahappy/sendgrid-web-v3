# v3.3 一键安装器更新

## 修复内容

- 自动检测 `apt`、`apt-get`、`dpkg` 和 `unattended-upgrades`。
- 自动等待四个常见 apt/dpkg 锁，默认最长 900 秒。
- `apt-get` 使用锁等待参数和网络下载重试。
- 软件包安装失败后自动执行 `dpkg --configure -a`，再重试最多 5 次。
- 应用安装阶段和 HTTPS 安装阶段使用同一套防锁逻辑。
- 下载 GitHub 源码时启用连接失败重试。
- 安装临时目录退出时自动清理。
- 一键命令统一使用 `sudo env ... bash`，避免非 root 管道执行问题。
- 重复安装继续保留现有 `.env`、数据库、上传文件与日志。

## 推荐命令

```bash
curl -fsSL https://raw.githubusercontent.com/odahappy/sendgrid-web-v3/main/sendgrid_web_admin_github_deploy/scripts/install_all.sh | sudo env DOMAIN=mailops.emailsender.mom EMAIL=allisonmcmurray@em1274.vpwsxl.com APP_PORT=9000 bash
```
