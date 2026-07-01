import os
import sqlite3
import hashlib
from .config import get_settings
from .utils import now_iso



def _hash_password(password):
    password = password or ""
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000
    ).hex()
    return "pbkdf2_sha256${}${}".format(salt, digest)


def db_path():
    path = get_settings().database_path
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return path


def get_conn():
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    # Reduce UI blocking when the background sender and the web page touch SQLite
    # at the same time. WAL allows readers and writers to coexist much better.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            service_type TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            remark TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            proxy_url_protected TEXT,
            status TEXT DEFAULT 'active',
            last_test_result TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS send_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            api_key_protected TEXT NOT NULL,
            from_email TEXT NOT NULL,
            from_name TEXT,
            proxy_id INTEGER,
            daily_limit INTEGER DEFAULT 500,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipient_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            list_group TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            name TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            UNIQUE(list_id, email)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipient_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            name TEXT,
            pool_type TEXT NOT NULL,
            status TEXT DEFAULT 'available',
            source_name TEXT,
            reserved_task_id INTEGER,
            reserved_schedule_id INTEGER,
            created_at TEXT,
            reserved_at TEXT,
            sent_at TEXT,
            updated_at TEXT,
            UNIQUE(tag_id, email, pool_type)
        )
    """)
    pool_cols = {row[1] for row in cur.execute("PRAGMA table_info(recipient_pool)").fetchall()}
    if "source_name" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN source_name TEXT")
    if "reserved_task_id" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN reserved_task_id INTEGER")
    if "reserved_schedule_id" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN reserved_schedule_id INTEGER")
    if "reserved_at" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN reserved_at TEXT")
    if "sent_at" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN sent_at TEXT")
    if "updated_at" not in pool_cols:
        cur.execute("ALTER TABLE recipient_pool ADD COLUMN updated_at TEXT")


    cur.execute("""
        CREATE TABLE IF NOT EXISTS template_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS template_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            has_unsubscribe INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mail_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            subject_template TEXT NOT NULL,
            template_group_id INTEGER NOT NULL,
            recipient_list_id INTEGER,
            batch1_list_id INTEGER NOT NULL,
            batch2_list_id INTEGER NOT NULL,
            batch1_start_days INTEGER DEFAULT 0,
            batch1_end_days INTEGER DEFAULT 3,
            batch2_start_days INTEGER DEFAULT 4,
            batch2_end_days INTEGER DEFAULT 30,
            status TEXT DEFAULT 'draft',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # Lightweight migration for builds before merged recipient lists.
    # Old SQLite files used batch1_list_id/batch2_list_id only. Keep those
    # columns for compatibility, but add recipient_list_id as the new semantic field.
    mail_task_cols = {row[1] for row in cur.execute("PRAGMA table_info(mail_tasks)").fetchall()}
    if "recipient_list_id" not in mail_task_cols:
        cur.execute("ALTER TABLE mail_tasks ADD COLUMN recipient_list_id INTEGER")
    cur.execute("""
        UPDATE mail_tasks
        SET recipient_list_id = batch1_list_id
        WHERE recipient_list_id IS NULL
          AND batch1_list_id IS NOT NULL
    """)

    # One task can now select multiple merged recipient lists.
    # Existing single-list/batch tasks are migrated into this mapping table so old
    # tasks still generate plans correctly.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mail_task_recipient_lists (
            task_id INTEGER NOT NULL,
            list_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT,
            PRIMARY KEY (task_id, list_id)
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO mail_task_recipient_lists (task_id, list_id, sort_order, created_at)
        SELECT id, recipient_list_id, 0, COALESCE(created_at, ?)
        FROM mail_tasks
        WHERE recipient_list_id IS NOT NULL
    """, (now_iso(),))
    cur.execute("""
        INSERT OR IGNORE INTO mail_task_recipient_lists (task_id, list_id, sort_order, created_at)
        SELECT id, batch1_list_id, 0, COALESCE(created_at, ?)
        FROM mail_tasks
        WHERE batch1_list_id IS NOT NULL
    """, (now_iso(),))
    cur.execute("""
        INSERT OR IGNORE INTO mail_task_recipient_lists (task_id, list_id, sort_order, created_at)
        SELECT id, batch2_list_id, 1, COALESCE(created_at, ?)
        FROM mail_tasks
        WHERE batch2_list_id IS NOT NULL
          AND batch2_list_id != batch1_list_id
    """, (now_iso(),))


    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_email_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            recipient_email TEXT NOT NULL,
            recipient_name TEXT,
            from_email TEXT NOT NULL,
            from_name TEXT,
            subject_template TEXT NOT NULL,
            subject_rendered TEXT NOT NULL,
            html_file TEXT NOT NULL,
            code8 TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            sender_response TEXT,
            created_at TEXT,
            sent_at TEXT
        )
    """)

    scheduled_cols = {row[1] for row in cur.execute("PRAGMA table_info(scheduled_email_tasks)").fetchall()}
    if "recipient_pool_id" not in scheduled_cols:
        cur.execute("ALTER TABLE scheduled_email_tasks ADD COLUMN recipient_pool_id INTEGER")
    if "recipient_pool_type" not in scheduled_cols:
        cur.execute("ALTER TABLE scheduled_email_tasks ADD COLUMN recipient_pool_type TEXT")


    cur.execute("""
        CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_task_id INTEGER,
            task_id INTEGER,
            channel_id INTEGER,
            proxy_id INTEGER,
            recipient_email TEXT,
            subject TEXT,
            http_status INTEGER,
            sendgrid_message_id TEXT,
            status TEXT,
            error_message TEXT,
            request_json TEXT,
            response_text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            last_error TEXT,
            UNIQUE(channel_id, date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sendgrid_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            event_type TEXT NOT NULL,
            timestamp_value TEXT,
            sg_message_id TEXT,
            smtp_id TEXT,
            reason TEXT,
            raw_json TEXT,
            created_at TEXT
        )
    """)



    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            role TEXT DEFAULT 'member',
            status TEXT DEFAULT 'active',
            last_login_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    user_count = cur.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if user_count == 0:
        settings = get_settings()
        cur.execute("""
            INSERT INTO users (
                username, password_hash, display_name, role, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, 'admin', 'active', ?, ?)
        """, (
            settings.admin_username,
            _hash_password(settings.admin_password),
            "System Admin",
            now_iso(), now_iso()
        ))

    cur.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_email_tasks(status, scheduled_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_pool ON scheduled_email_tasks(recipient_pool_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_group_detail ON scheduled_email_tasks(tag_id, task_id, channel_id, from_email, scheduled_at, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_send_log_group_detail ON send_log(task_id, channel_id, created_at, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recipient_pool_available ON recipient_pool(tag_id, pool_type, status, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recipient_pool_task ON recipient_pool(reserved_task_id, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recipients_list ON recipients(list_id, email)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_recipient_lists_task ON mail_task_recipient_lists(task_id, sort_order)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_recipient_lists_list ON mail_task_recipient_lists(list_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON sendgrid_events(event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_msg ON sendgrid_events(sg_message_id)")
    conn.commit()
    conn.close()


def q_all(sql, params=()):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def q_one(sql, params=()):
    conn = get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else None


def execute(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id


def execute_rowcount(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def execute_many(sql, rows):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(sql, rows)
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def today():
    return now_iso()[:10]
