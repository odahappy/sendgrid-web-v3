SendGrid Web Admin Scheduler - Recipient Pool Warm-up Build
Python 3.9 compatible

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

3. Recipient pool upload changed
   - Recipients are now managed in two reusable pools:
     * 0-3天库: used for day 1, day 2 and day 3
     * 4-30天库: used for day 4 through day 30
   - New tasks no longer select recipient lists manually.
   - When a plan is generated, the system automatically selects recipients from the two pools based on the selected tag.
   - Recipients added into a plan are marked as reserved and disappear from the available pool.
   - The database does not physically delete recipients; it uses statuses: available / reserved / sent / failed.
   - If pool stock is insufficient, the page shows a popup alert and no plan is created.
   - Recipient pool upload supports selecting multiple TXT/CSV files at once.
   - Recipient pool upload no longer enforces MAX_RECIPIENT_UPLOAD_BYTES.

4. Plan generation warm-up speed
   - Day 1: no more than 60 emails, randomized to 45-60
   - Day 2: no more than 200 emails, randomized to 185-200
   - Day 3: no more than 700 emails, randomized to 685-700
   - Day 4-30: no more than 1000 emails per day, randomized to 985-1000/day
   - The random 5-15 email difference never exceeds the configured upper bound.
   - Day 4 now correctly starts at offset 3 internally, so there is no blank/misaligned fourth day.

5. Pool state handling
   - Task deletion releases pending/sending pool emails back to available.
   - Sent emails stay sent.
   - Failed emails stay failed and are not automatically returned to available.
   - Regeneration is blocked if a task already has sent records, preventing repeated sending.

6. UI features preserved
   - API channel edit/update
   - HTML source popup TXT editor
   - Task page and schedule page separated
   - Schedule details grouped by date tabs
   - Send logs grouped by task/account with date-tab details
   - Dashboard statistics:
     sent, delivered, opens, clicks, blocks, spam reports
   - SendGrid Event Webhook endpoint:
     POST /api/sendgrid/events?token=YOUR_SERVICE_TOKEN

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
   SECRET_KEY=your random secret
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
