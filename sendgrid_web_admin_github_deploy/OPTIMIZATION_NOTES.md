# SendGrid Web Admin v3.2 优化说明

本版本在不清空现有 SQLite 数据的前提下，重点修复任务排期、并发一致性、发送恢复、密钥保护和部署安全问题。

## 关键变化

- 收件人池新增分页明细管理，可按邮箱、姓名、来源和状态搜索。
- 未使用的 `available` 收件人支持修改标签、库、邮箱、姓名和来源备注，也可单条删除或批量清空。
- 已被计划使用的 `reserved`、`sent`、`failed` 收件人只允许修改姓名和来源备注，禁止改变邮箱归属或删除，避免破坏计划和审计记录。
- 计划生成使用 `BEGIN IMMEDIATE` 原子事务，收件人选择、预留和计划写入不可分割。
- 强制重生成在全部校验和资源分配成功后才替换旧计划；失败会完整回滚。
- 每日计划数量受通道 `daily_limit` 和该日期已有计划共同约束，不再先超排再长期顺延。
- 跨两个收件人池按小写邮箱去重，避免同一地址在一个任务中重复使用。
- 删除或重生成时检测 `sending` 记录，不再释放正在发送的收件人。
- 增加发送领取时间、worker 标识、通道容量槽位及超时恢复；过期领取会释放占用的容量。
- 通道每日容量使用原子预留，支持未来扩展为多进程 worker。
- API Key 和代理密码改用 AES-GCM；旧版 XOR 密文仍可读取，更新记录后自动写入新格式。
- Session 每次请求重新检查用户状态和角色；最后一个有效管理员不能被停用或降级。
- 增加登录失败速率限制、SameSite/Secure Cookie 配置及跨来源 POST 检查。
- 收件人上传恢复单文件和文件数量限制；Nginx 请求体上限改为 50 MB。
- Webhook 增加请求体限制、常量时间 Token 比较和事件去重。
- 默认不再把完整邮件 HTML 请求体写入 `send_log`。
- 健康检查现在验证 SQLite 和后台 worker 状态。
- SQLite 备份改用官方 Backup API，兼容 WAL 模式下的在线备份。
- HTTPS 配置后，后端强制监听 `127.0.0.1`，并移除应用端口的 UFW 放行。
- 相同模板文件名使用不同磁盘存储名，不再互相覆盖。

## 升级注意事项

旧 `.env` 没有 `ENVIRONMENT=production` 时可兼容启动。建议补充并生成以下配置：

```dotenv
ENVIRONMENT=production
SECRET_KEY=<随机字符串，至少32字符>
DATA_ENCRYPTION_KEY=<另一条随机字符串，至少32字符>
SERVICE_TOKEN=<随机字符串，至少24字符>
SESSION_COOKIE_SECURE=true
ALLOW_WEBHOOK_QUERY_TOKEN=false
```

`DATA_ENCRYPTION_KEY` 必须与 `SECRET_KEY` 不同。新版安装脚本会自动生成这两个独立密钥。

SendGrid 若仍通过 URL 查询参数传递 Token，需要临时设置 `ALLOW_WEBHOOK_QUERY_TOKEN=true`；更推荐由反向代理注入 `X-INTERNAL-TOKEN`，或后续接入 SendGrid Signed Event Webhook。

## 运行测试

```bash
python -m unittest -v tests.test_reliability
```
