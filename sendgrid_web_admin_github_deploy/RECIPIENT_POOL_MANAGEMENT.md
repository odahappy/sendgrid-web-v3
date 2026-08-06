# 收件人池编辑与删除功能

版本：v3.2

## 新增功能

- 在“收件人池/库”统计表中增加“管理收件人”按钮。
- 收件人明细按需分页加载，避免大规模收件人池导致页面卡顿。
- 支持按邮箱、姓名、来源备注搜索。
- 支持按 `available`、`reserved`、`sent`、`failed` 状态筛选。
- 支持每页显示 20、50、100 或 200 条记录。
- 支持编辑单个收件人的标签、库类型、邮箱、姓名和来源备注。
- 支持删除单个未使用收件人。
- 支持批量清空当前标签和库中的全部未使用 `available` 收件人。

## 数据安全规则

为了避免破坏已生成的发送计划和历史审计记录：

- 状态为 `available` 且从未进入发送计划的记录，可修改全部字段，也可删除。
- 已被计划引用的 `reserved`、`sent`、`failed` 记录，只允许修改姓名和来源备注。
- 已使用记录不能修改标签、库类型或邮箱。
- 已使用记录不能物理删除。
- 批量清空只删除未被计划引用的 `available` 记录，不影响已占用、已发送和失败记录。
- 所有更新和删除都使用 `BEGIN IMMEDIATE` 事务，并在状态变化或唯一邮箱冲突时回滚。

## 新增接口

- `GET /api/recipients/pool/rows`
- `POST /recipients/{recipient_id}/update`
- `POST /recipients/{recipient_id}/delete`
- `POST /recipients/pool/delete-available`

所有接口都要求登录，并受现有同源 POST 防护保护。

## 验证结果

已通过：

- 6 项自动化可靠性测试。
- Python 全项目编译检查。
- Jinja2 模板解析检查。
- 浏览器端 JavaScript 语法检查。
- FastAPI 路由注册检查。
- 登录、收件人查询、编辑和删除的 Web 集成测试。
