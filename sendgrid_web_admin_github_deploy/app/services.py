import json
import random
import os
import hashlib
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit
import time

import requests

from .config import get_settings
from .crypto import protect, unprotect
from .db import q_all, q_one, execute, execute_many, execute_rowcount, today, get_conn
from .utils import now_iso, parse_recipient_line, code8, render_vars


SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"



def normalize_proxy_url(raw_proxy_url):
    """Normalize common proxy inputs into a requests-compatible proxy URL.

    Supported examples:
    - 209.166.41.146:7723:username:password -> http://username:password@209.166.41.146:7723
    - 209.166.41.146:7723 -> http://209.166.41.146:7723
    - http://user:pass@ip:port -> kept as-is
    - socks5://user:pass@ip:port -> kept as-is, requires requests[socks]
    """
    raw = (raw_proxy_url or "").strip()
    if not raw:
        return ""

    # Already a standard proxy URL accepted by requests.
    if "://" in raw:
        return raw

    parts = raw.split(":")
    if len(parts) == 4:
        host, port, username, password = [x.strip() for x in parts]
        if host and port and username:
            return "http://{}:{}@{}:{}".format(
                quote(username, safe=""),
                quote(password, safe=""),
                host,
                port,
            )

    if len(parts) == 2:
        host, port = [x.strip() for x in parts]
        if host and port:
            return "http://{}:{}".format(host, port)

    # Keep unknown format unchanged so the tester can return the real error message.
    return raw


def mask_proxy_url(proxy_url):
    url = normalize_proxy_url(proxy_url)
    if not url:
        return "-"
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        host = parsed.hostname or ""
        port = (":{}".format(parsed.port)) if parsed.port else ""
        if parsed.username or parsed.password:
            netloc = "***:***@{}{}".format(host, port)
        else:
            netloc = "{}{}".format(host, port)
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except Exception:
        return url


def test_proxy_url(proxy_url, timeout=12):
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return {
            "ok": False,
            "normalized_url": "",
            "masked_url": "-",
            "message": "FAIL：代理地址为空。",
        }

    proxies = {"http": normalized, "https": normalized}
    test_urls = [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
        "http://httpbin.org/ip",
    ]
    errors = []
    for url in test_urls:
        start = time.time()
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout)
            elapsed_ms = int((time.time() - start) * 1000)
            body = (resp.text or "")[:500].replace("\n", " ")
            exit_ip = ""
            try:
                data = resp.json()
                exit_ip = data.get("ip") or data.get("origin") or ""
            except Exception:
                pass
            if 200 <= resp.status_code < 300:
                msg = "OK：代理测试成功；出口IP：{}；耗时：{}ms；格式：{}".format(
                    exit_ip or "未知", elapsed_ms, mask_proxy_url(normalized)
                )
                return {
                    "ok": True,
                    "normalized_url": normalized,
                    "masked_url": mask_proxy_url(normalized),
                    "http_status": resp.status_code,
                    "elapsed_ms": elapsed_ms,
                    "exit_ip": exit_ip,
                    "message": msg,
                }
            errors.append("{} HTTP {} {}".format(url, resp.status_code, body[:160]))
        except Exception as exc:
            errors.append("{} {}: {}".format(url, exc.__class__.__name__, str(exc)[:220]))

    msg = "FAIL：代理测试失败；格式：{}；错误：{}".format(
        mask_proxy_url(normalized), " | ".join(errors[-2:])
    )
    return {
        "ok": False,
        "normalized_url": normalized,
        "masked_url": mask_proxy_url(normalized),
        "message": msg,
    }



def hash_password(password):
    password = password or ""
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000
    ).hex()
    return "pbkdf2_sha256${}${}".format(salt, digest)


def verify_password(password, stored_hash):
    if not stored_hash or not stored_hash.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, salt, digest = stored_hash.split("$", 2)
        check = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt.encode("utf-8"), 200000
        ).hex()
        return check == digest
    except Exception:
        return False


def authenticate_user(username, password):
    user = q_one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    if not user:
        return None
    if user.get("status") != "active":
        return None
    if not verify_password(password, user.get("password_hash")):
        return None
    execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), user["id"]))
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user.get("role") or "member",
    }


def create_user(username, password, display_name, role, status):
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    role = role if role in ("admin", "member") else "member"
    status = status if status in ("active", "disabled") else "active"
    return execute("""
        INSERT INTO users (username, password_hash, display_name, role, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (username, hash_password(password), display_name or username, role, status, now_iso(), now_iso()))


def update_user(user_id, display_name, role, status, password=None):
    role = role if role in ("admin", "member") else "member"
    status = status if status in ("active", "disabled") else "active"
    if password and password.strip():
        execute("""
            UPDATE users
            SET display_name=?, role=?, status=?, password_hash=?, updated_at=?
            WHERE id=?
        """, (display_name, role, status, hash_password(password.strip()), now_iso(), user_id))
    else:
        execute("""
            UPDATE users
            SET display_name=?, role=?, status=?, updated_at=?
            WHERE id=?
        """, (display_name, role, status, now_iso(), user_id))


def create_tag(name, service_type, remark):
    return execute("""
        INSERT INTO tags (name, service_type, remark, status, created_at, updated_at)
        VALUES (?, ?, ?, 'active', ?, ?)
    """, (name, service_type, remark, now_iso(), now_iso()))


def create_proxy(name, proxy_url):
    normalized_url = normalize_proxy_url(proxy_url)
    return execute("""
        INSERT INTO proxies (name, proxy_url_protected, status, last_test_result, created_at, updated_at)
        VALUES (?, ?, 'active', ?, ?, ?)
    """, (name, protect(normalized_url), "未测试；格式：{}".format(mask_proxy_url(normalized_url)), now_iso(), now_iso()))


def get_proxies_for_dashboard():
    rows = q_all("SELECT * FROM proxies ORDER BY id DESC")
    for row in rows:
        raw = unprotect(row.get("proxy_url_protected") or "")
        normalized = normalize_proxy_url(raw)
        row["proxy_url_preview"] = mask_proxy_url(normalized)
    return rows


def test_proxy(proxy_id):
    proxy = q_one("SELECT * FROM proxies WHERE id=?", (proxy_id,))
    if not proxy:
        raise ValueError("代理不存在。")
    raw = unprotect(proxy.get("proxy_url_protected") or "")
    result = test_proxy_url(raw)
    # If the old stored value was the compact host:port:user:pass format, save the normalized URL.
    execute("""
        UPDATE proxies
        SET proxy_url_protected=?, last_test_result=?, updated_at=?
        WHERE id=?
    """, (protect(result.get("normalized_url") or normalize_proxy_url(raw)), result.get("message") or "测试完成", now_iso(), proxy_id))
    return result


def create_channel(tag_id, name, api_key, from_email, from_name, proxy_id, daily_limit):
    return execute("""
        INSERT INTO send_channels (
            tag_id, name, api_key_protected, from_email, from_name,
            proxy_id, daily_limit, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
    """, (
        tag_id, name, protect(api_key), from_email, from_name,
        proxy_id if proxy_id else None, daily_limit, now_iso(), now_iso()
    ))


def update_channel(channel_id, tag_id, name, api_key, from_email, from_name, proxy_id, daily_limit, status):
    if api_key and api_key.strip():
        execute("""
            UPDATE send_channels
            SET tag_id=?, name=?, api_key_protected=?, from_email=?, from_name=?,
                proxy_id=?, daily_limit=?, status=?, updated_at=?
            WHERE id=?
        """, (tag_id, name, protect(api_key.strip()), from_email, from_name, proxy_id if proxy_id else None,
              daily_limit, status, now_iso(), channel_id))
    else:
        execute("""
            UPDATE send_channels
            SET tag_id=?, name=?, from_email=?, from_name=?,
                proxy_id=?, daily_limit=?, status=?, updated_at=?
            WHERE id=?
        """, (tag_id, name, from_email, from_name, proxy_id if proxy_id else None,
              daily_limit, status, now_iso(), channel_id))


def create_recipient_list(tag_id, name, list_group):
    return execute("""
        INSERT INTO recipient_lists (tag_id, name, list_group, status, created_at, updated_at)
        VALUES (?, ?, ?, 'active', ?, ?)
    """, (tag_id, name, list_group, now_iso(), now_iso()))


def import_recipients(list_id, content_bytes):
    text = content_bytes.decode("utf-8", errors="ignore")
    rows = []
    seen = set()
    invalid = 0
    for line in text.splitlines():
        parsed = parse_recipient_line(line)
        if not parsed:
            if line.strip() and not line.strip().startswith("#"):
                invalid += 1
            continue
        email, name = parsed
        if email in seen:
            continue
        seen.add(email)
        rows.append((list_id, email, name, "active", now_iso()))

    if rows:
        execute_many("""
            INSERT OR IGNORE INTO recipients (list_id, email, name, status, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, rows)
    return {"imported": len(rows), "invalid": invalid}


def create_template_group(tag_id, name):
    return execute("""
        INSERT INTO template_groups (tag_id, name, status, created_at, updated_at)
        VALUES (?, ?, 'active', ?, ?)
    """, (tag_id, name, now_iso(), now_iso()))


def update_template_group(group_id, tag_id, name, status):
    status = status if status in ("active", "paused", "disabled") else "active"
    return execute_rowcount("""
        UPDATE template_groups
        SET tag_id=?, name=?, status=?, updated_at=?
        WHERE id=?
    """, (tag_id, name, status, now_iso(), group_id))


def delete_template_group(group_id):
    used = q_one("SELECT COUNT(*) AS c FROM mail_tasks WHERE template_group_id=?", (group_id,))
    if used and int(used["c"]) > 0:
        raise ValueError("Template group is used by mail tasks and cannot be deleted.")

    execute("DELETE FROM template_files WHERE group_id=?", (group_id,))
    deleted = execute_rowcount("DELETE FROM template_groups WHERE id=?", (group_id,))

    settings = get_settings()
    folder = Path(settings.template_storage_dir) / str(group_id)
    if folder.exists():
        shutil.rmtree(folder)

    return deleted


def save_template_file(group_id, filename, content_bytes):
    settings = get_settings()
    safe_name = filename.replace("\\", "_").replace("/", "_")
    folder = Path(settings.template_storage_dir) / str(group_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe_name
    path.write_bytes(content_bytes)
    text = content_bytes.decode("utf-8", errors="ignore").lower()
    has_unsub = 1 if ("unsubscribe" in text or "退订" in text) else 0
    return execute("""
        INSERT INTO template_files (group_id, filename, file_path, has_unsubscribe, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (group_id, safe_name, str(path), has_unsub, now_iso()))


def _read_template_file_for_editor(file_path):
    try:
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return "<!-- 读取模板文件失败：{} -->".format(str(exc))


def get_template_groups_with_files():
    groups = q_all("""
        SELECT g.*, t.name AS tag_name,
               (SELECT COUNT(*) FROM template_files f WHERE f.group_id=g.id) AS file_count
        FROM template_groups g
        LEFT JOIN tags t ON t.id=g.tag_id
        ORDER BY g.id DESC
    """)
    for group in groups:
        files = q_all("""
            SELECT id, group_id, filename, file_path, has_unsubscribe, created_at
            FROM template_files
            WHERE group_id=?
            ORDER BY id ASC
        """, (group["id"],))
        for item in files:
            content = _read_template_file_for_editor(item.get("file_path") or "")
            item["content"] = content
            item["content_size"] = len(content.encode("utf-8", errors="ignore"))
            item["modal_id"] = "templateFileEdit{}".format(item["id"])
        group["files"] = files
    return groups


def update_template_file_content(file_id, html_content):
    row = q_one("SELECT * FROM template_files WHERE id=?", (file_id,))
    if not row:
        raise ValueError("Template file not found")

    content = html_content or ""
    content_bytes = content.encode("utf-8")
    max_bytes = get_settings().max_template_upload_bytes
    if len(content_bytes) > max_bytes:
        raise ValueError("HTML content is too large. Max allowed is {} bytes.".format(max_bytes))

    path = Path(row["file_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    low = content.lower()
    has_unsub = 1 if ("unsubscribe" in low or "退订" in low) else 0
    execute_rowcount("UPDATE template_files SET has_unsubscribe=? WHERE id=?", (has_unsub, file_id))
    return {"ok": True, "id": file_id, "bytes": len(content_bytes)}



POOL_0_3 = "warmup_0_3"
POOL_4_30 = "warmup_4_30"
POOL_TYPES = (POOL_0_3, POOL_4_30)
POOL_NAMES = {
    POOL_0_3: "0-3天库（第1-3天）",
    POOL_4_30: "4-30天库（第4-30天）",
}


def _pool_name(pool_type):
    return POOL_NAMES.get(pool_type, pool_type or "未知库")


def _normalize_pool_type(pool_type):
    value = (pool_type or "").strip()
    if value not in POOL_TYPES:
        raise ValueError("Unknown recipient pool type")
    return value


def import_recipient_pool(tag_id, pool_type, source_name, content_bytes):
    """Import recipients into one of the two reusable recipient pools.

    The upload-size limit is intentionally not enforced for recipient pools.
    Duplicate emails inside the same tag + pool are ignored by the UNIQUE index.
    """
    pool_type = _normalize_pool_type(pool_type)
    tag = q_one("SELECT id FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise ValueError("Tag not found")

    text = content_bytes.decode("utf-8", errors="ignore")
    source_name = (source_name or "").strip() or _pool_name(pool_type)
    rows = []
    seen = set()
    invalid = 0
    for line in text.splitlines():
        parsed = parse_recipient_line(line)
        if not parsed:
            if line.strip() and not line.strip().startswith("#"):
                invalid += 1
            continue
        email, name = parsed
        email_key = email.strip().lower()
        if not email_key or email_key in seen:
            continue
        seen.add(email_key)
        rows.append((
            int(tag_id), email_key, name, pool_type, "available",
            source_name, now_iso(), now_iso(),
        ))

    before = _count(
        "SELECT COUNT(*) AS c FROM recipient_pool WHERE tag_id=? AND pool_type=?",
        (tag_id, pool_type),
    )
    if rows:
        execute_many("""
            INSERT OR IGNORE INTO recipient_pool (
                tag_id, email, name, pool_type, status, source_name, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    after = _count(
        "SELECT COUNT(*) AS c FROM recipient_pool WHERE tag_id=? AND pool_type=?",
        (tag_id, pool_type),
    )
    return {
        "parsed": len(rows),
        "imported": max(0, after - before),
        "duplicates": max(0, len(rows) - max(0, after - before)),
        "invalid": invalid,
        "pool_type": pool_type,
    }


def get_recipient_pool_stats():
    """Return tag + pool summaries for the recipient-pool page."""
    rows = q_all("""
        SELECT
            t.id AS tag_id,
            t.name AS tag_name,
            p.pool_type,
            COUNT(p.id) AS total_count,
            SUM(CASE WHEN p.status='available' THEN 1 ELSE 0 END) AS available_count,
            SUM(CASE WHEN p.status='reserved' THEN 1 ELSE 0 END) AS reserved_count,
            SUM(CASE WHEN p.status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN p.status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MAX(p.created_at) AS last_import_at
        FROM tags t
        LEFT JOIN recipient_pool p ON p.tag_id=t.id
        GROUP BY t.id, p.pool_type
        ORDER BY t.id DESC, p.pool_type ASC
    """)

    result = []
    seen = set()
    for row in rows:
        tag_id = row.get("tag_id")
        pool_type = row.get("pool_type")
        if pool_type not in POOL_TYPES:
            continue
        seen.add((tag_id, pool_type))
        row["pool_name"] = _pool_name(pool_type)
        for key in ("total_count", "available_count", "reserved_count", "sent_count", "failed_count"):
            row[key] = int(row.get(key) or 0)
        result.append(row)

    # Make every tag show both pools, even when one pool is empty.
    tags = q_all("SELECT id, name FROM tags ORDER BY id DESC")
    for tag in tags:
        for pool_type in POOL_TYPES:
            if (tag["id"], pool_type) in seen:
                continue
            result.append({
                "tag_id": tag["id"],
                "tag_name": tag["name"],
                "pool_type": pool_type,
                "pool_name": _pool_name(pool_type),
                "total_count": 0,
                "available_count": 0,
                "reserved_count": 0,
                "sent_count": 0,
                "failed_count": 0,
                "last_import_at": None,
            })
    result.sort(key=lambda x: (-(int(x.get("tag_id") or 0)), x.get("pool_type") or ""))
    return result


def _validate_task_resources(tag_id, channel_id, template_group_id):
    """Ensure channel and template group belong to the selected tag."""
    tag_id = int(tag_id)
    tag = q_one("SELECT id FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise ValueError("Tag not found")

    channel = q_one("SELECT id, tag_id FROM send_channels WHERE id=?", (channel_id,))
    if not channel:
        raise ValueError("Channel not found")
    if int(channel["tag_id"]) != tag_id:
        raise ValueError("Selected channel does not belong to the task tag")

    group = q_one("SELECT id, tag_id FROM template_groups WHERE id=?", (template_group_id,))
    if not group:
        raise ValueError("Template group not found")
    if int(group["tag_id"]) != tag_id:
        raise ValueError("Selected template group does not belong to the task tag")


def create_mail_task(tag_id, channel_id, name, subject_template, template_group_id):
    """Create a task that automatically consumes the two recipient pools at plan time."""
    _validate_task_resources(tag_id, channel_id, template_group_id)
    return execute("""
        INSERT INTO mail_tasks (
            tag_id, channel_id, name, subject_template, template_group_id,
            recipient_list_id, batch1_list_id, batch2_list_id,
            batch1_start_days, batch1_end_days,
            batch2_start_days, batch2_end_days,
            status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 2, 3, 29, 'draft', ?, ?)
    """, (
        tag_id, channel_id, name, subject_template, template_group_id,
        now_iso(), now_iso()
    ))


def _random_time(start_dt, end_dt):
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    if end_ts <= start_ts:
        return start_dt
    return datetime.fromtimestamp(random.randint(start_ts, end_ts))


def _warmup_base_limit(day_offset):
    """Return the requested warm-up baseline for a day offset.

    day_offset=0 means the first day of the plan. The current business rule is:
    first day <= 60, second day <= 200, third day <= 700, day 4-30 <= 1000.
    """
    if int(day_offset) <= 0:
        return 60
    if int(day_offset) == 1:
        return 200
    if int(day_offset) == 2:
        return 700
    return 1000


def _warmup_daily_limit(day_offset):
    """Build a randomized daily limit while keeping the requested upper bound.

    Final ranges:
      day 1: 45-60, day 2: 185-200, day 3: 685-700, day 4+: 985-1000.
    """
    base = _warmup_base_limit(day_offset)
    delta = random.randint(5, 15)
    if random.choice((True, False)):
        return max(1, base - delta)
    return base


def _build_30_day_warmup_limits():
    return {day_offset: _warmup_daily_limit(day_offset) for day_offset in range(30)}


def _day_window(plan_start, day_offset):
    """Return the valid random-send window for one plan day."""
    day_start = plan_start.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=int(day_offset))
    day_end = plan_start.replace(hour=22, minute=59, second=59, microsecond=0) + timedelta(days=int(day_offset))
    if int(day_offset) == 0 and day_start < plan_start:
        day_start = plan_start + timedelta(minutes=1)
    if day_end <= day_start:
        day_end = day_start + timedelta(minutes=30)
    return day_start, day_end


def _pool_need_by_type(limit_by_day):
    need_0_3 = sum(int(limit_by_day.get(offset, 0)) for offset in range(0, 3))
    need_4_30 = sum(int(limit_by_day.get(offset, 0)) for offset in range(3, 30))
    return {POOL_0_3: need_0_3, POOL_4_30: need_4_30}


def _available_pool_count(tag_id, pool_type):
    return _count(
        "SELECT COUNT(*) AS c FROM recipient_pool WHERE tag_id=? AND pool_type=? AND status='available'",
        (tag_id, pool_type),
    )


def _check_pool_stock_or_raise(tag_id, need_by_type):
    errors = []
    available = {}
    for pool_type, need in need_by_type.items():
        count = _available_pool_count(tag_id, pool_type)
        available[pool_type] = count
        if count < int(need):
            errors.append("{} 可用 {} 个，需要 {} 个，缺少 {} 个".format(
                _pool_name(pool_type), count, int(need), int(need) - count
            ))
    if errors:
        raise ValueError("收件人池库存不足：" + "；".join(errors))
    return available


def _release_pool_reservations_for_task(task_id):
    """Release unsent reservations before deleting/regenerating a task.

    Pending/sending emails go back to available. Sent emails stay sent. Final failed
    emails stay failed and are not returned automatically.
    """
    execute("""
        UPDATE recipient_pool
        SET status='available', reserved_task_id=NULL, reserved_schedule_id=NULL,
            reserved_at=NULL, updated_at=?
        WHERE id IN (
            SELECT recipient_pool_id FROM scheduled_email_tasks
            WHERE task_id=? AND status IN ('pending', 'sending') AND recipient_pool_id IS NOT NULL
        )
    """, (now_iso(), task_id))
    execute("""
        UPDATE recipient_pool
        SET status='failed', updated_at=?
        WHERE id IN (
            SELECT recipient_pool_id FROM scheduled_email_tasks
            WHERE task_id=? AND status='failed' AND recipient_pool_id IS NOT NULL
        )
    """, (now_iso(), task_id))
    execute("""
        UPDATE recipient_pool
        SET status='sent', sent_at=COALESCE(sent_at, ?), updated_at=?
        WHERE id IN (
            SELECT recipient_pool_id FROM scheduled_email_tasks
            WHERE task_id=? AND status='sent' AND recipient_pool_id IS NOT NULL
        )
    """, (now_iso(), now_iso(), task_id))


def generate_plan(task_id, force=False):
    task = q_one("SELECT * FROM mail_tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task not found")

    existing = int(q_one("SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=?", (task_id,))["c"])
    if existing and not force:
        return {"ok": False, "message": "Plan already exists. Use force to recreate.", "existing": existing}

    sent_existing = int(q_one(
        "SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=? AND status='sent'",
        (task_id,),
    )["c"])
    if force and sent_existing > 0:
        raise ValueError("该任务已经有已发送记录，不能直接重生成。请新建任务，避免重复发送。")

    channel = q_one("SELECT * FROM send_channels WHERE id=?", (task["channel_id"],))
    if not channel:
        raise ValueError("Channel not found")

    templates = q_all("SELECT * FROM template_files WHERE group_id=?", (task["template_group_id"],))
    if not templates:
        raise ValueError("No template files in selected group")

    if force:
        _release_pool_reservations_for_task(task_id)
        execute("DELETE FROM scheduled_email_tasks WHERE task_id=? AND status IN ('pending', 'sending', 'failed')", (task_id,))

    now = datetime.now()
    limit_by_day = _build_30_day_warmup_limits()
    need_by_type = _pool_need_by_type(limit_by_day)
    _check_pool_stock_or_raise(task["tag_id"], need_by_type)

    # Pull recipients from the two available pools only after all stock checks pass.
    selected = []
    selected_by_pool = {}
    for pool_type, need in need_by_type.items():
        rows = q_all("""
            SELECT id, email, name, pool_type
            FROM recipient_pool
            WHERE tag_id=? AND pool_type=? AND status='available'
            ORDER BY id ASC
            LIMIT ?
        """, (task["tag_id"], pool_type, int(need)))
        if len(rows) < int(need):
            raise ValueError("{} 可用邮箱不足。".format(_pool_name(pool_type)))
        selected_by_pool[pool_type] = rows

    for day_offset in range(30):
        pool_type = POOL_0_3 if day_offset <= 2 else POOL_4_30
        day_recipients = selected_by_pool[pool_type][:limit_by_day[day_offset]]
        selected_by_pool[pool_type] = selected_by_pool[pool_type][limit_by_day[day_offset]:]
        for rec in day_recipients:
            selected.append((day_offset, rec))

    conn = get_conn()
    try:
        cur = conn.cursor()
        rows = []
        for day_offset, rec in selected:
            day_start, day_end = _day_window(now, day_offset)
            tfile = random.choice(templates)
            c8 = code8()
            variables = {
                "from_mail": channel["from_email"],
                "to_email": rec["email"],
                "code8": c8,
            }
            subject_rendered = render_vars(task["subject_template"], variables)
            scheduled_at = _random_time(day_start, day_end).isoformat(timespec="seconds")
            row = (
                task["id"], task["tag_id"], task["channel_id"],
                rec["email"], rec.get("name") or "",
                channel["from_email"], channel.get("from_name") or "",
                task["subject_template"], subject_rendered,
                tfile["file_path"], c8, scheduled_at,
                "pending", 0, now_iso(), rec["id"], rec["pool_type"]
            )
            rows.append(row)

        cur.executemany("""
            INSERT INTO scheduled_email_tasks (
                task_id, tag_id, channel_id, recipient_email, recipient_name,
                from_email, from_name, subject_template, subject_rendered,
                html_file, code8, scheduled_at, status, attempts, created_at,
                recipient_pool_id, recipient_pool_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

        # Mark the selected pool records as reserved. They disappear from the available pool.
        pool_ids = [rec["id"] for _, rec in selected]
        if pool_ids:
            placeholders = ",".join("?" for _ in pool_ids)
            cur.execute("""
                UPDATE recipient_pool
                SET status='reserved', reserved_task_id=?, reserved_at=?, updated_at=?
                WHERE id IN ({}) AND status='available'
            """.format(placeholders), (task_id, now_iso(), now_iso(), *pool_ids))

        cur.execute("UPDATE mail_tasks SET status='planned', updated_at=? WHERE id=?", (now_iso(), task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "ok": True,
        "created": len(rows),
        "need_0_3": need_by_type[POOL_0_3],
        "need_4_30": need_by_type[POOL_4_30],
        "daily_limits": limit_by_day,
    }

def start_task(task_id):
    execute("UPDATE mail_tasks SET status='running', updated_at=? WHERE id=?", (now_iso(), task_id))


def pause_task(task_id):
    execute("UPDATE mail_tasks SET status='paused', updated_at=? WHERE id=?", (now_iso(), task_id))


def resume_task(task_id):
    execute("UPDATE mail_tasks SET status='running', updated_at=? WHERE id=?", (now_iso(), task_id))


def delete_mail_task(task_id):
    _release_pool_reservations_for_task(task_id)
    execute("DELETE FROM scheduled_email_tasks WHERE task_id=?", (task_id,))
    execute("DELETE FROM mail_task_recipient_lists WHERE task_id=?", (task_id,))
    return execute_rowcount("DELETE FROM mail_tasks WHERE id=?", (task_id,))


def _count(sql, params=()):
    row = q_one(sql, params)
    return int(row["c"]) if row else 0


def _make_date_groups(rows, datetime_field, id_prefix):
    """Group detail rows by date for date-tab detail modals."""
    groups = []
    group_map = {}
    for item in rows:
        raw_value = item.get(datetime_field) or ""
        date_key = raw_value[:10] if raw_value else "未知日期"
        if date_key not in group_map:
            index = len(groups) + 1
            group = {
                "date": date_key,
                "count": 0,
                "tab_id": "{}Tab{}".format(id_prefix, index),
                "pane_id": "{}Pane{}".format(id_prefix, index),
                "rows": [],
            }
            group_map[date_key] = group
            groups.append(group)
        group = group_map[date_key]
        group["rows"].append(item)
        group["count"] += 1
    return groups


def get_sendgrid_metrics():
    return {
        "sent": _count("SELECT COUNT(*) AS c FROM send_log WHERE status='sent'"),
        "delivered": _count("SELECT COUNT(*) AS c FROM sendgrid_events WHERE event_type='delivered'"),
        "opened": _count("SELECT COUNT(*) AS c FROM sendgrid_events WHERE event_type='open'"),
        "clicked": _count("SELECT COUNT(*) AS c FROM sendgrid_events WHERE event_type='click'"),
        "rejected": _count("SELECT COUNT(*) AS c FROM sendgrid_events WHERE event_type IN ('bounce','dropped','blocked')"),
        "complaints": _count("SELECT COUNT(*) AS c FROM sendgrid_events WHERE event_type='spamreport'"),
    }


def get_schedule_tag_groups():
    """Build schedule summaries grouped by tag, then by account/channel plan.

    Important performance rule: do NOT load every single scheduled email into the
    main admin page. Large plans can contain tens of thousands of rows. Details
    are loaded on demand by the /api/schedule/detail/* endpoints.
    """
    plan_rows = q_all("""
        SELECT
            s.tag_id,
            COALESCE(t.name, '未命名标签') AS tag_name,
            COALESCE(t.service_type, '') AS service_type,
            s.task_id,
            COALESCE(m.name, '未命名任务') AS task_name,
            s.channel_id,
            COALESCE(c.name, '未命名通道') AS channel_name,
            s.from_email,
            COALESCE(s.from_name, '') AS from_name,
            COUNT(*) AS total_count,
            SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN s.status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MIN(s.scheduled_at) AS first_scheduled_at,
            MAX(s.scheduled_at) AS last_scheduled_at
        FROM scheduled_email_tasks s
        LEFT JOIN tags t ON t.id=s.tag_id
        LEFT JOIN mail_tasks m ON m.id=s.task_id
        LEFT JOIN send_channels c ON c.id=s.channel_id
        GROUP BY s.tag_id, s.task_id, s.channel_id, s.from_email
        ORDER BY tag_name ASC, channel_name ASC, first_scheduled_at ASC
    """)

    tag_map = {}
    for index, row in enumerate(plan_rows, start=1):
        row["pending_count"] = int(row.get("pending_count") or 0)
        row["sent_count"] = int(row.get("sent_count") or 0)
        row["failed_count"] = int(row.get("failed_count") or 0)
        row["total_count"] = int(row.get("total_count") or 0)
        row["detail_key"] = "schedule-{}-{}-{}".format(row["tag_id"], row["task_id"], row["channel_id"])

        tag_id = row["tag_id"]
        if tag_id not in tag_map:
            tag_map[tag_id] = {
                "tag_id": tag_id,
                "tag_name": row.get("tag_name") or "未命名标签",
                "service_type": row.get("service_type") or "",
                "total_count": 0,
                "pending_count": 0,
                "sent_count": 0,
                "failed_count": 0,
                "plans": [],
            }
        tag_group = tag_map[tag_id]
        tag_group["total_count"] += row["total_count"]
        tag_group["pending_count"] += row["pending_count"]
        tag_group["sent_count"] += row["sent_count"]
        tag_group["failed_count"] += row["failed_count"]
        tag_group["plans"].append(row)

    return list(tag_map.values())


def get_schedule_detail_dates(tag_id, task_id, channel_id, from_email):
    rows = q_all("""
        SELECT
            substr(scheduled_at, 1, 10) AS date,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MIN(scheduled_at) AS first_time,
            MAX(scheduled_at) AS last_time
        FROM scheduled_email_tasks
        WHERE tag_id=? AND task_id=? AND channel_id=? AND from_email=?
        GROUP BY substr(scheduled_at, 1, 10)
        ORDER BY date ASC
    """, (tag_id, task_id, channel_id, from_email))
    for row in rows:
        row["total_count"] = int(row.get("total_count") or 0)
        row["pending_count"] = int(row.get("pending_count") or 0)
        row["sent_count"] = int(row.get("sent_count") or 0)
        row["failed_count"] = int(row.get("failed_count") or 0)
    return rows


def get_schedule_detail_rows(tag_id, task_id, channel_id, from_email, date_value):
    return q_all("""
        SELECT
            id, scheduled_at, recipient_email, from_email, subject_rendered,
            code8, html_file, status, COALESCE(last_error, '') AS last_error
        FROM scheduled_email_tasks
        WHERE tag_id=? AND task_id=? AND channel_id=? AND from_email=?
          AND substr(scheduled_at, 1, 10)=?
        ORDER BY scheduled_at ASC, id ASC
    """, (tag_id, task_id, channel_id, from_email, date_value))



def get_log_task_groups():
    """Build send-log summaries grouped by single task and account/channel.

    Important performance rule: do NOT load every send_log row into the main
    admin page. Details are loaded on demand by /api/logs/detail/* endpoints.
    """
    group_rows = q_all("""
        SELECT
            l.task_id,
            COALESCE(m.name, '未绑定任务') AS task_name,
            COALESCE(m.tag_id, c.tag_id, 0) AS tag_id,
            COALESCE(t.name, '未命名标签') AS tag_name,
            l.channel_id,
            COALESCE(c.name, '未命名通道') AS channel_name,
            COALESCE(c.from_email, '') AS from_email,
            COUNT(*) AS total_count,
            SUM(CASE WHEN l.status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN l.status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MIN(l.created_at) AS first_created_at,
            MAX(l.created_at) AS last_created_at
        FROM send_log l
        LEFT JOIN mail_tasks m ON m.id=l.task_id
        LEFT JOIN send_channels c ON c.id=l.channel_id
        LEFT JOIN tags t ON t.id=COALESCE(m.tag_id, c.tag_id)
        GROUP BY l.task_id, l.channel_id
        ORDER BY last_created_at DESC
        LIMIT 100
    """)

    result = []
    for index, row in enumerate(group_rows, start=1):
        row["total_count"] = int(row.get("total_count") or 0)
        row["sent_count"] = int(row.get("sent_count") or 0)
        row["failed_count"] = int(row.get("failed_count") or 0)
        row["detail_key"] = "log-{}-{}".format(row.get("task_id") or "null", row.get("channel_id") or "null")
        result.append(row)
    return result


def get_log_detail_dates(task_id, channel_id):
    rows = q_all("""
        SELECT
            substr(created_at, 1, 10) AS date,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MIN(created_at) AS first_time,
            MAX(created_at) AS last_time
        FROM send_log
        WHERE ((? IS NULL AND task_id IS NULL) OR task_id=?)
          AND ((? IS NULL AND channel_id IS NULL) OR channel_id=?)
        GROUP BY substr(created_at, 1, 10)
        ORDER BY date DESC
    """, (task_id, task_id, channel_id, channel_id))
    for row in rows:
        row["total_count"] = int(row.get("total_count") or 0)
        row["sent_count"] = int(row.get("sent_count") or 0)
        row["failed_count"] = int(row.get("failed_count") or 0)
    return rows


def get_log_detail_rows(task_id, channel_id, date_value):
    return q_all("""
        SELECT
            l.id, l.created_at, l.recipient_email, l.subject, l.http_status,
            l.sendgrid_message_id, l.status, COALESCE(l.error_message, '') AS error_message,
            COALESCE(c.name, '未命名通道') AS channel_name,
            COALESCE(p.name, '-') AS proxy_name
        FROM send_log l
        LEFT JOIN send_channels c ON c.id=l.channel_id
        LEFT JOIN proxies p ON p.id=l.proxy_id
        WHERE ((? IS NULL AND l.task_id IS NULL) OR l.task_id=?)
          AND ((? IS NULL AND l.channel_id IS NULL) OR l.channel_id=?)
          AND substr(l.created_at, 1, 10)=?
        ORDER BY l.created_at DESC, l.id DESC
    """, (task_id, task_id, channel_id, channel_id, date_value))



def _to_int_fields(row, fields):
    """Convert SQLite aggregate values to plain ints for JSON/UI use."""
    for field in fields:
        row[field] = int(row.get(field) or 0)
    return row


def get_tag_detail(tag_id):
    """Return one tag/project overview without loading massive row details.

    The tag detail modal should show the whole content under a tag, but it must
    remain lightweight. Therefore this function returns summaries and related
    object lists rather than every scheduled email or every send log row.
    """
    tag = q_one("SELECT * FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise ValueError("标签不存在")

    tag_id = int(tag_id)

    counters = q_one("""
        SELECT
            (SELECT COUNT(*) FROM send_channels WHERE tag_id=?) AS channels_total,
            (SELECT COUNT(*) FROM send_channels WHERE tag_id=? AND status='active') AS channels_active,
            (SELECT COUNT(*) FROM template_groups WHERE tag_id=?) AS template_groups_total,
            (SELECT COUNT(*) FROM template_files f JOIN template_groups g ON g.id=f.group_id WHERE g.tag_id=?) AS template_files_total,
            (SELECT COUNT(*) FROM mail_tasks WHERE tag_id=?) AS tasks_total,
            (SELECT COUNT(*) FROM mail_tasks WHERE tag_id=? AND status='running') AS tasks_running,
            (SELECT COUNT(*) FROM scheduled_email_tasks WHERE tag_id=?) AS scheduled_total,
            (SELECT COUNT(*) FROM scheduled_email_tasks WHERE tag_id=? AND status='pending') AS scheduled_pending,
            (SELECT COUNT(*) FROM scheduled_email_tasks WHERE tag_id=? AND status='sent') AS scheduled_sent,
            (SELECT COUNT(*) FROM scheduled_email_tasks WHERE tag_id=? AND status='failed') AS scheduled_failed,
            (SELECT COUNT(*) FROM recipient_pool WHERE tag_id=?) AS pool_total,
            (SELECT COUNT(*) FROM recipient_pool WHERE tag_id=? AND status='available') AS pool_available,
            (SELECT COUNT(*) FROM recipient_pool WHERE tag_id=? AND status='reserved') AS pool_reserved,
            (SELECT COUNT(*) FROM recipient_pool WHERE tag_id=? AND status='sent') AS pool_sent,
            (SELECT COUNT(*) FROM recipient_pool WHERE tag_id=? AND status='failed') AS pool_failed,
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=?) AS logs_total,
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=? AND l.status='sent') AS logs_sent,
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=? AND l.status='failed') AS logs_failed
    """, (tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id, tag_id)) or {}
    _to_int_fields(counters, [
        "channels_total", "channels_active", "template_groups_total", "template_files_total",
        "tasks_total", "tasks_running", "scheduled_total", "scheduled_pending",
        "scheduled_sent", "scheduled_failed", "pool_total", "pool_available", "pool_reserved",
        "pool_sent", "pool_failed", "logs_total", "logs_sent", "logs_failed"
    ])

    pool_rows = q_all("""
        SELECT
            pool_type,
            CASE pool_type WHEN 'warmup_0_3' THEN '0-3天库' WHEN 'warmup_4_30' THEN '4-30天库' ELSE pool_type END AS pool_name,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status='available' THEN 1 ELSE 0 END) AS available_count,
            SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS reserved_count,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MAX(created_at) AS last_import_at,
            MAX(updated_at) AS last_updated_at
        FROM recipient_pool
        WHERE tag_id=?
        GROUP BY pool_type
        ORDER BY pool_type
    """, (tag_id,))
    pool_by_type = {r.get("pool_type"): r for r in pool_rows}
    pools = []
    for pool_type, pool_name in [("warmup_0_3", "0-3天库"), ("warmup_4_30", "4-30天库")]:
        row = pool_by_type.get(pool_type) or {
            "pool_type": pool_type,
            "pool_name": pool_name,
            "total_count": 0,
            "available_count": 0,
            "reserved_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "last_import_at": None,
            "last_updated_at": None,
        }
        row["pool_name"] = pool_name
        _to_int_fields(row, ["total_count", "available_count", "reserved_count", "sent_count", "failed_count"])
        pools.append(row)

    channels = q_all("""
        SELECT
            c.id, c.name, c.from_email, COALESCE(c.from_name, '') AS from_name,
            c.daily_limit, c.status, c.created_at, c.updated_at,
            COALESCE(p.name, '无') AS proxy_name,
            COALESCE(p.status, '') AS proxy_status,
            COALESCE((SELECT sent_count FROM channel_daily_stats ds WHERE ds.channel_id=c.id AND ds.date=date('now','localtime')), 0) AS today_sent,
            (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.channel_id=c.id) AS scheduled_count,
            (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.channel_id=c.id AND s.status='pending') AS pending_count,
            (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.channel_id=c.id AND s.status='sent') AS sent_count,
            (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.channel_id=c.id AND s.status='failed') AS failed_count
        FROM send_channels c
        LEFT JOIN proxies p ON p.id=c.proxy_id
        WHERE c.tag_id=?
        ORDER BY c.id DESC
    """, (tag_id,))
    for row in channels:
        _to_int_fields(row, ["daily_limit", "today_sent", "scheduled_count", "pending_count", "sent_count", "failed_count"])

    template_groups = q_all("""
        SELECT
            g.id, g.name, g.status, g.created_at, g.updated_at,
            COUNT(f.id) AS file_count,
            GROUP_CONCAT(f.filename, ', ') AS filenames
        FROM template_groups g
        LEFT JOIN template_files f ON f.group_id=g.id
        WHERE g.tag_id=?
        GROUP BY g.id
        ORDER BY g.id DESC
    """, (tag_id,))
    for row in template_groups:
        _to_int_fields(row, ["file_count"])
        row["filenames"] = row.get("filenames") or ""

    tasks = q_all("""
        SELECT
            m.id, m.name, m.subject_template, m.status, m.created_at, m.updated_at,
            COALESCE(c.name, '未命名通道') AS channel_name,
            COALESCE(c.from_email, '') AS from_email,
            COALESCE(g.name, '未命名模板组') AS template_group_name,
            COUNT(s.id) AS scheduled_count,
            SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN s.status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) AS failed_count,
            MIN(s.scheduled_at) AS first_scheduled_at,
            MAX(s.scheduled_at) AS last_scheduled_at
        FROM mail_tasks m
        LEFT JOIN send_channels c ON c.id=m.channel_id
        LEFT JOIN template_groups g ON g.id=m.template_group_id
        LEFT JOIN scheduled_email_tasks s ON s.task_id=m.id
        WHERE m.tag_id=?
        GROUP BY m.id
        ORDER BY m.id DESC
    """, (tag_id,))
    for row in tasks:
        _to_int_fields(row, ["scheduled_count", "pending_count", "sent_count", "failed_count"])

    schedule_by_date = q_all("""
        SELECT
            substr(scheduled_at, 1, 10) AS date,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count
        FROM scheduled_email_tasks
        WHERE tag_id=?
        GROUP BY substr(scheduled_at, 1, 10)
        ORDER BY date ASC
        LIMIT 60
    """, (tag_id,))
    for row in schedule_by_date:
        _to_int_fields(row, ["total_count", "pending_count", "sent_count", "failed_count"])

    logs_by_date = q_all("""
        SELECT
            substr(l.created_at, 1, 10) AS date,
            COUNT(*) AS total_count,
            SUM(CASE WHEN l.status='sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN l.status='failed' THEN 1 ELSE 0 END) AS failed_count
        FROM send_log l
        LEFT JOIN mail_tasks m ON m.id=l.task_id
        LEFT JOIN send_channels c ON c.id=l.channel_id
        WHERE COALESCE(m.tag_id, c.tag_id)=?
        GROUP BY substr(l.created_at, 1, 10)
        ORDER BY date DESC
        LIMIT 30
    """, (tag_id,))
    for row in logs_by_date:
        _to_int_fields(row, ["total_count", "sent_count", "failed_count"])

    return {
        "tag": tag,
        "counters": counters,
        "pools": pools,
        "channels": channels,
        "template_groups": template_groups,
        "tasks": tasks,
        "schedule_by_date": schedule_by_date,
        "logs_by_date": logs_by_date,
    }


def get_dashboard_data():
    return {
        "tags": q_all("SELECT * FROM tags ORDER BY id DESC"),
        "users": q_all("""
            SELECT id, username, display_name, role, status, last_login_at, created_at, updated_at
            FROM users
            ORDER BY id ASC
        """),
        "channels": q_all("""
            SELECT c.*, t.name AS tag_name, p.name AS proxy_name,
                   COALESCE((SELECT sent_count FROM channel_daily_stats ds WHERE ds.channel_id=c.id AND ds.date=date('now','localtime')), 0) AS today_sent
            FROM send_channels c
            LEFT JOIN tags t ON t.id=c.tag_id
            LEFT JOIN proxies p ON p.id=c.proxy_id
            ORDER BY c.id DESC
        """),
        "proxies": get_proxies_for_dashboard(),
        "recipient_lists": q_all("""
            SELECT l.*, t.name AS tag_name,
                   (SELECT COUNT(*) FROM recipients r WHERE r.list_id=l.id) AS recipient_count
            FROM recipient_lists l
            LEFT JOIN tags t ON t.id=l.tag_id
            ORDER BY l.id DESC
        """),
        "recipient_pool_stats": get_recipient_pool_stats(),
        "template_groups": get_template_groups_with_files(),
        "tasks": q_all("""
            SELECT m.*, t.name AS tag_name, c.name AS channel_name,
                   '自动收件人池' AS recipient_list_name,
                   2 AS recipient_list_count,
                   (SELECT COUNT(*) FROM recipient_pool p WHERE p.tag_id=m.tag_id AND p.status='available') AS recipient_count,
                   (SELECT COUNT(*) FROM recipient_pool p WHERE p.tag_id=m.tag_id AND p.pool_type='warmup_0_3' AND p.status='available') AS pool_0_3_available,
                   (SELECT COUNT(*) FROM recipient_pool p WHERE p.tag_id=m.tag_id AND p.pool_type='warmup_4_30' AND p.status='available') AS pool_4_30_available,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id) AS scheduled_count,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id AND s.status='sent') AS sent_count,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id AND s.status='failed') AS failed_count
            FROM mail_tasks m
            LEFT JOIN tags t ON t.id=m.tag_id
            LEFT JOIN send_channels c ON c.id=m.channel_id
            ORDER BY m.id DESC
        """),
        "schedule": q_all("""
            SELECT s.*, c.name AS channel_name, t.name AS tag_name
            FROM scheduled_email_tasks s
            LEFT JOIN send_channels c ON c.id=s.channel_id
            LEFT JOIN tags t ON t.id=s.tag_id
            ORDER BY s.scheduled_at ASC
            LIMIT 200
        """),
        "schedule_tag_groups": get_schedule_tag_groups(),
        "logs": q_all("""
            SELECT l.*, c.name AS channel_name, p.name AS proxy_name
            FROM send_log l
            LEFT JOIN send_channels c ON c.id=l.channel_id
            LEFT JOIN proxies p ON p.id=l.proxy_id
            ORDER BY l.id DESC
            LIMIT 200
        """),
        "log_task_groups": get_log_task_groups(),
        "stats": {
            "total_scheduled": _count("SELECT COUNT(*) AS c FROM scheduled_email_tasks"),
            "total_sent": _count("SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE status='sent'"),
            "total_pending": _count("SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE status='pending'"),
            "total_failed": _count("SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE status='failed'"),
            "active_channels": _count("SELECT COUNT(*) AS c FROM send_channels WHERE status='active'"),
            "active_users": _count("SELECT COUNT(*) AS c FROM users WHERE status='active'"),
        },
        "sg_metrics": get_sendgrid_metrics(),
    }


def channel_daily_sent(channel_id):
    row = q_one("""
        SELECT sent_count FROM channel_daily_stats
        WHERE channel_id=? AND date=?
    """, (channel_id, today()))
    return int(row["sent_count"]) if row else 0


def increment_channel_stat(channel_id, sent_ok, error=None):
    row = q_one("SELECT id FROM channel_daily_stats WHERE channel_id=? AND date=?", (channel_id, today()))
    if not row:
        execute("""
            INSERT INTO channel_daily_stats (channel_id, date, sent_count, failed_count, last_error)
            VALUES (?, ?, 0, 0, NULL)
        """, (channel_id, today()))
    if sent_ok:
        execute("""
            UPDATE channel_daily_stats
            SET sent_count = sent_count + 1
            WHERE channel_id=? AND date=?
        """, (channel_id, today()))
    else:
        execute("""
            UPDATE channel_daily_stats
            SET failed_count = failed_count + 1, last_error=?
            WHERE channel_id=? AND date=?
        """, (error, channel_id, today()))


def _build_proxies(channel):
    if not channel.get("proxy_id"):
        return None
    proxy = q_one("SELECT * FROM proxies WHERE id=? AND status='active'", (channel["proxy_id"],))
    if not proxy:
        return None
    proxy_url = normalize_proxy_url(unprotect(proxy["proxy_url_protected"]))
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _log_send_attempt(scheduled, channel, subject, http_status, msg_id, status, error, request_json, response_text):
    execute("""
        INSERT INTO send_log (
            scheduled_task_id, task_id, channel_id, proxy_id, recipient_email,
            subject, http_status, sendgrid_message_id, status, error_message,
            request_json, response_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        scheduled["id"], scheduled["task_id"], channel["id"] if channel else scheduled.get("channel_id"),
        channel.get("proxy_id") if channel else None, scheduled["recipient_email"],
        subject, http_status, msg_id, status, error, request_json, response_text, now_iso()
    ))


def _send_via_sendgrid(scheduled):
    settings = get_settings()
    channel = q_one("SELECT * FROM send_channels WHERE id=?", (scheduled["channel_id"],))
    if not channel:
        return False, None, "Channel not found", None

    try:
        if channel["status"] != "active":
            return False, None, "Channel is not active", None

        sent_today = channel_daily_sent(channel["id"])
        if sent_today >= int(channel["daily_limit"]):
            return False, None, "Channel daily limit reached", None

        html = Path(scheduled["html_file"]).read_text(encoding="utf-8", errors="ignore")
        variables = {
            "from_mail": scheduled["from_email"],
            "to_email": scheduled["recipient_email"],
            "code8": scheduled["code8"],
        }
        html_rendered = render_vars(html, variables)
        subject_rendered = render_vars(scheduled["subject_template"], variables)

        tag = q_one("SELECT * FROM tags WHERE id=?", (scheduled["tag_id"],))
        if settings.require_unsubscribe_for_marketing and tag and tag["service_type"] == "marketing":
            low = html_rendered.lower()
            if "unsubscribe" not in low and "退订" not in low:
                return False, None, "Marketing HTML does not contain unsubscribe keyword/link", None

        payload = {
            "personalizations": [
                {
                    "to": [{"email": scheduled["recipient_email"]}],
                    "custom_args": {
                        "source": "web_admin_scheduler",
                        "scheduled_task_id": str(scheduled["id"]),
                        "task_id": str(scheduled["task_id"]),
                        "channel_id": str(channel["id"]),
                        "code8": scheduled["code8"],
                    }
                }
            ],
            "from": {
                "email": scheduled["from_email"],
                "name": scheduled["from_name"] or channel.get("from_name") or "",
            },
            "subject": subject_rendered,
            "content": [{"type": "text/html", "value": html_rendered}],
        }

        headers = {
            "Authorization": "Bearer {}".format(unprotect(channel["api_key_protected"])),
            "Content-Type": "application/json",
        }

        resp = requests.post(
            SENDGRID_URL,
            headers=headers,
            json=payload,
            proxies=_build_proxies(channel),
            timeout=settings.request_timeout_seconds,
        )
        msg_id = resp.headers.get("X-Message-Id") or resp.headers.get("x-message-id")
        ok = resp.status_code in (200, 202)
        err = None if ok else resp.text
        _log_send_attempt(
            scheduled, channel, subject_rendered, resp.status_code, msg_id,
            "sent" if ok else "failed", err,
            json.dumps(payload, ensure_ascii=False), resp.text
        )
        increment_channel_stat(channel["id"], ok, err)
        return ok, msg_id, err, resp.text
    except Exception as exc:
        error = str(exc)
        _log_send_attempt(
            scheduled, channel, scheduled["subject_rendered"], None, None,
            "failed", error, None, None
        )
        increment_channel_stat(channel["id"], False, error)
        return False, None, error, None


def process_due_tasks(limit):
    now_value = now_iso()
    due = q_all("""
        SELECT s.*
        FROM scheduled_email_tasks s
        JOIN mail_tasks m ON m.id=s.task_id
        WHERE s.status='pending'
          AND m.status='running'
          AND s.scheduled_at <= ?
        ORDER BY s.scheduled_at ASC, s.id ASC
        LIMIT ?
    """, (now_value, limit))

    processed = 0
    for task in due:
        claimed = execute_rowcount("""
            UPDATE scheduled_email_tasks
            SET status='sending'
            WHERE id=?
              AND status='pending'
              AND scheduled_at <= ?
              AND EXISTS (
                  SELECT 1
                  FROM mail_tasks m
                  WHERE m.id=scheduled_email_tasks.task_id
                    AND m.status='running'
              )
        """, (task["id"], now_value))
        if claimed != 1:
            continue
        ok, msg_id, err, raw = _send_via_sendgrid(task)
        if ok:
            sent_at = now_iso()
            execute("""
                UPDATE scheduled_email_tasks
                SET status='sent', sent_at=?, sender_response=?
                WHERE id=?
            """, (sent_at, raw or msg_id or "", task["id"]))
            if task.get("recipient_pool_id"):
                execute("""
                    UPDATE recipient_pool
                    SET status='sent', sent_at=?, updated_at=?
                    WHERE id=?
                """, (sent_at, sent_at, task["recipient_pool_id"]))
        else:
            if err == "Channel daily limit reached":
                retry_at = (datetime.now() + timedelta(days=1)).replace(hour=8, minute=5, second=0, microsecond=0).isoformat(timespec="seconds")
                execute("""
                    UPDATE scheduled_email_tasks
                    SET status='pending', scheduled_at=?, last_error=?, sender_response=?
                    WHERE id=?
                """, (retry_at, err, raw, task["id"]))
            else:
                attempts = int(task["attempts"]) + 1
                if attempts >= 3:
                    status = "failed"
                else:
                    status = "pending"
                retry_at = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
                execute("""
                    UPDATE scheduled_email_tasks
                    SET status=?, attempts=?, scheduled_at=?, last_error=?, sender_response=?
                    WHERE id=?
                """, (status, attempts, retry_at, err, raw, task["id"]))
                if status == "failed" and task.get("recipient_pool_id"):
                    execute("""
                        UPDATE recipient_pool
                        SET status='failed', updated_at=?
                        WHERE id=?
                    """, (now_iso(), task["recipient_pool_id"]))
        processed += 1
    return processed


def record_sendgrid_events(payload):
    if isinstance(payload, dict):
        events = [payload]
    elif isinstance(payload, list):
        events = payload
    else:
        events = []

    rows = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        rows.append((
            ev.get("email"),
            ev.get("event") or ev.get("event_type") or "unknown",
            str(ev.get("timestamp") or ""),
            ev.get("sg_message_id") or ev.get("sg_message-id") or ev.get("sendgrid_message_id"),
            ev.get("smtp-id") or ev.get("smtp_id"),
            ev.get("reason") or ev.get("response") or ev.get("status"),
            json.dumps(ev, ensure_ascii=False),
            now_iso(),
        ))
    if rows:
        execute_many("""
            INSERT INTO sendgrid_events (
                email, event_type, timestamp_value, sg_message_id, smtp_id,
                reason, raw_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)
