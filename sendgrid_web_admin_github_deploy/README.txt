SendGrid Web Admin Scheduler - Single Recipient Pool and Template Metadata
Python 3.9+ compatible

See WARMUP_GUIDE.md for current warm-up behavior. OPTIMIZATION_NOTES.md
describes the earlier v3.2 release and is retained as a historical record.

This build includes:

1. Team member management
   - Admin can add users
   - Admin can edit user display name
   - Admin can reset user password
   - Admin can set role: admin / member
   - Admin can enable or disable users
   - Passwords are stored as salted PBKDF2 hashes, not plaintext

2. Login uses database users
   - On first database initialization, the default admin user is created from .env:
     ADMIN_USERNAME
     ADMIN_PASSWORD

3. One recipient pool
   - Upload TXT/CSV files into one shared pool. A recipient is counted once per tag.
   - Named lists are filters over this pool, not separate recipient inventories.
   - The fixed 30-day task takes available recipients for its tag from this pool.
   - A custom warm-up task can use the full pool or a named-list filter.
   - Recipients added into a plan are marked as reserved and disappear from the available pool.
   - Unused available recipients can be edited or physically deleted from the management page.
   - Used reserved / sent / failed recipients are retained for schedule and audit integrity.
   - If pool stock is insufficient, the page shows a popup alert and no plan is created.
   - Recipient pool upload supports selecting multiple TXT/CSV files at once.
   - Recipient pool upload supports multiple files and enforces configurable per-file/count limits.
   - Pool detail management supports pagination, search, individual edit/delete, and bulk deletion of unused available rows.
   - Upgrading preserves existing plans, reservations, send records and named-list membership.

4. HTML template metadata
   - At upload, enter a subject and sender display name once. Both values are
     initially bound to every HTML file in that upload; edit files individually later.
   - A template group may contain multiple files with different subjects and sender names.
   - The sender email address and API credentials still come from the selected API channel.
   - New plans use the selected file's bound subject and sender name. Existing scheduled
     emails keep the values already recorded in their plans. Older template files
     without bindings use the existing task subject and channel sender name.

5. Fixed 30-day plan generation speed
   - Day 1: no more than 60 emails, randomized to 45-60
   - Day 2: no more than 200 emails, randomized to 185-200
   - Day 3: no more than 700 emails, randomized to 685-700
   - Day 4-30: requested warm-up ceiling is 1000 emails per day.
   - Every day is additionally capped by the channel daily limit and existing channel plans.
   - The random 5-15 email difference never exceeds either effective upper bound.
   - Day 4 now correctly starts at offset 3 internally, so there is no blank/misaligned fourth day.

6. Pool state handling
   - Task deletion releases pending pool emails; deletion/regeneration is blocked while a send is active.
   - Sent emails stay sent.
   - Failed emails stay failed and are not automatically returned to available.
   - Regeneration is blocked if a task already has sent records, preventing repeated sending.

7. UI features preserved
   - API channel edit/update
   - HTML source popup TXT editor
   - Task page and schedule page separated
   - Schedule details grouped by date tabs
   - Send logs grouped by task/account with date-tab details
   - Dashboard statistics:
     sent, delivered, opens, clicks, blocks, spam reports
   - SendGrid Event Webhook endpoint:
     POST /api/sendgrid/events with X-INTERNAL-TOKEN (query token is optional and disabled by default)

First install:
Double click install_all_safe.bat

Normal start:
Double click start_web_admin.bat

Open:
http://127.0.0.1:8080

Default login if you did not change .env:
admin / admin123456

Recommended:
1. Edit .env before first start:
   ADMIN_PASSWORD=your strong password
   SECRET_KEY=your random session secret
   DATA_ENCRYPTION_KEY=a different random data key
   SERVICE_TOKEN=your random token
2. Start the service.
3. Login as admin.
4. Open 团队成员 and add your team members.

Upgrade:
1. Stop old service.
2. Extract this package.
3. Copy old .env into this folder.
4. Copy old data folder into this folder if you want to keep old data.
5. Run install_all_safe.bat.
6. Run start_web_admin.bat.

Important:
Only use opted-in or legitimate recipients.
For marketing emails, include unsubscribe links.

Custom warm-up system:
- 标签 / API 通道 / 代理 now share one Settings page.
- Warm-up tasks have user-entered 1–90 days, required daily counts (0 is a rest day),
  automatic 24-hour spacing or a manual interval, and selectable recipient filters
  and HTML template groups. First Start fixes the 24-hour clock; saving a draft does not.
- One pool serves both fixed 30-day and custom warm-up tasks. A named list filters
  eligible addresses in that pool and never creates another pool.
- Unsent items that miss their 24-hour window are skipped; uncertain sends require
  administrator review. HTTP 202 means accepted by SendGrid, not delivered.
- See WARMUP_GUIDE.md for setup, limits, status meanings, and upgrade behavior.
