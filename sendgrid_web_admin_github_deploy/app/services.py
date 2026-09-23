import json
import random
import os
import hashlib
import hmac
import shutil
import sqlite3
import uuid
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit
import time

import requests

from .config import get_settings
from .crypto import protect, unprotect
from .db import q_all, q_one, execute, execute_many, execute_rowcount, today, get_conn
from .utils import now_iso, parse_recipient_line, code8, render_vars, is_valid_email


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
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def authenticate_user(username, password):
    user = q_one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password, user.get("password_hash")):
        return None
    execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), user["id"]))
    return _session_user(user)


def _session_user(user):
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user.get("role") or "member",
    }


def get_active_user_session(user_id):
    user = q_one("SELECT * FROM users WHERE id=? AND status='active'", (user_id,))
    return _session_user(user) if user else None


def create_user(username, password, display_name, role, status):
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    if len(password) < 8:
        raise ValueError("Password must contain at least 8 characters")
    role = role if role in ("admin", "member") else "member"
    status = status if status in ("active", "disabled") else "active"
    return execute("""
        INSERT INTO users (username, password_hash, display_name, role, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (username, hash_password(password), display_name or username, role, status, now_iso(), now_iso()))


def update_user(user_id, display_name, role, status, password=None):
    role = role if role in ("admin", "member") else "member"
    status = status if status in ("active", "disabled") else "active"
    current = q_one("SELECT id, role, status FROM users WHERE id=?", (user_id,))
    if not current:
        raise ValueError("User not found")
    removes_active_admin = (
        current.get("role") == "admin"
        and current.get("status") == "active"
        and (role != "admin" or status != "active")
    )
    if removes_active_admin:
        active_admins = _count("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='active'")
        if active_admins <= 1:
            raise ValueError("不能停用或降级最后一个有效管理员。")

    if password and password.strip():
        if len(password.strip()) < 8:
            raise ValueError("Password must contain at least 8 characters")
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
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        channel = conn.execute("SELECT tag_id FROM send_channels WHERE id=?", (channel_id,)).fetchone()
        if not channel:
            raise ValueError("通道不存在。")
        if not conn.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone():
            raise ValueError("标签不存在。")
        if int(channel["tag_id"]) != int(tag_id) and conn.execute("""
            SELECT 1 FROM mail_tasks WHERE channel_id=? LIMIT 1
        """, (channel_id,)).fetchone():
            raise ValueError("通道已被任务使用，不能更改所属标签。")
        values = [tag_id, name, from_email, from_name, proxy_id if proxy_id else None,
                  daily_limit, status, now_iso()]
        key_clause = ""
        if api_key and api_key.strip():
            key_clause = ", api_key_protected=?"
            values.append(protect(api_key.strip()))
        values.append(channel_id)
        conn.execute("""
            UPDATE send_channels SET tag_id=?,name=?,from_email=?,from_name=?,
                proxy_id=?,daily_limit=?,status=?,updated_at=?{} WHERE id=?
        """.format(key_clause), tuple(values))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    name = (name or "").strip()
    if not name or len(name) > 180:
        raise ValueError("请填写有效的模板组名称（最多 180 个字符）。")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        group = conn.execute("SELECT tag_id FROM template_groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise ValueError("模板组不存在。")
        tag = conn.execute("SELECT id FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not tag:
            raise ValueError("标签不存在。")
        if int(group["tag_id"]) != int(tag_id):
            used = conn.execute(
                "SELECT 1 FROM mail_tasks WHERE template_group_id=? LIMIT 1", (group_id,)
            ).fetchone()
            if used:
                raise ValueError("模板组已被任务使用，不能更改所属标签。")
        cursor = conn.execute("""
            UPDATE template_groups SET tag_id=?, name=?, status=?, updated_at=? WHERE id=?
        """, (tag_id, name, status, now_iso(), group_id))
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def validate_template_metadata(subject_template, from_name):
    """Validate the two mail headers associated with one HTML template file."""
    values = []
    for raw, label, maximum in (
        (subject_template, "邮件主题", 500),
        (from_name, "发件人名称", 180),
    ):
        value = "" if raw is None else str(raw)
        if any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in value):
            raise ValueError("{}不能包含换行或控制字符。".format(label))
        value = value.strip()
        if not value or len(value) > maximum:
            raise ValueError("请填写有效的{}（最多 {} 个字符）。".format(label, maximum))
        values.append(value)
    return tuple(values)


def _template_file_data(filename, content_bytes):
    safe_name = (filename or "").strip()
    safe_name = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char
                        for char in safe_name)[:180]
    if not safe_name or Path(safe_name).suffix.lower() not in (".html", ".htm"):
        raise ValueError("仅支持非空的 HTML/HTM 模板文件。")
    if not isinstance(content_bytes, bytes) or not content_bytes:
        raise ValueError("HTML 模板文件不能为空。")
    if len(content_bytes) > get_settings().max_template_upload_bytes:
        raise ValueError("HTML 模板文件超过上传大小限制。")
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("HTML 模板文件必须使用 UTF-8 编码。") from exc
    has_unsub = int("unsubscribe" in text.lower() or "退订" in text)
    return safe_name, content_bytes, has_unsub


def _insert_template_file(cur, folder, group_id, file_data, subject_template, from_name):
    safe_name, content_bytes, has_unsub = file_data
    # A new path preserves the bytes referenced by already scheduled messages.
    path = folder / (uuid.uuid4().hex + "_" + safe_name)
    try:
        path.write_bytes(content_bytes)
        cursor = cur.execute("""
            INSERT INTO template_files
                (group_id, filename, file_path, subject_template, from_name,
                 has_unsubscribe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (group_id, safe_name, str(path), subject_template, from_name,
              has_unsub, now_iso()))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return cursor.lastrowid, path


def upload_template_group(tag_id, name, files, subject_template, from_name):
    """Store a whole multi-file upload or leave neither rows nor files behind."""
    subject_template, from_name = validate_template_metadata(subject_template, from_name)
    name = (name or "").strip()
    if not name or len(name) > 180:
        raise ValueError("请填写有效的模板组名称（最多 180 个字符）。")
    settings = get_settings()
    files = list(files)
    if not files or len(files) > settings.max_template_files_per_upload:
        raise ValueError("HTML 模板文件数量不符合上传限制。")
    prepared = [_template_file_data(filename, content) for filename, content in files]
    conn = get_conn()
    created_paths = []
    folder = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        tag = conn.execute("SELECT id, status FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not tag or tag["status"] != "active":
            raise ValueError("请选择有效且启用的标签。")
        stamp = now_iso()
        cursor = conn.execute("""
            INSERT INTO template_groups (tag_id, name, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
        """, (tag_id, name, stamp, stamp))
        group_id = cursor.lastrowid
        folder = Path(settings.template_storage_dir) / str(group_id)
        folder.mkdir(parents=True, exist_ok=True)
        for item in prepared:
            _, path = _insert_template_file(
                conn, folder, group_id, item, subject_template, from_name
            )
            created_paths.append(path)
        conn.commit()
        return group_id
    except Exception:
        conn.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        if folder is not None:
            try:
                folder.rmdir()
            except OSError:
                pass
        raise
    finally:
        conn.close()


def save_template_file(group_id, filename, content_bytes, subject_template=None, from_name=None):
    """Backward compatible single-file importer, including legacy NULL metadata."""
    if (subject_template is None) != (from_name is None):
        raise ValueError("邮件主题和发件人名称必须同时填写。")
    if subject_template is not None:
        subject_template, from_name = validate_template_metadata(subject_template, from_name)
    file_data = _template_file_data(filename, content_bytes)
    conn = get_conn()
    path = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM template_groups WHERE id=?", (group_id,)).fetchone():
            raise ValueError("模板组不存在。")
        folder = Path(get_settings().template_storage_dir) / str(group_id)
        folder.mkdir(parents=True, exist_ok=True)
        file_id, path = _insert_template_file(
            conn, folder, group_id, file_data, subject_template, from_name
        )
        conn.commit()
        return file_id
    except Exception:
        conn.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


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
            SELECT id, group_id, filename, file_path, subject_template, from_name,
                   has_unsubscribe, created_at
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


def update_template_file_content(file_id, html_content, subject_template=None, from_name=None):
    """Edit a template without changing HTML already referenced by a schedule."""
    if (subject_template is None) != (from_name is None):
        raise ValueError("邮件主题和发件人名称必须同时填写。")
    if subject_template is not None:
        subject_template, from_name = validate_template_metadata(subject_template, from_name)
    content = html_content or ""
    content_bytes = content.encode("utf-8")
    max_bytes = get_settings().max_template_upload_bytes
    if not content_bytes:
        raise ValueError("HTML 模板内容不能为空。")
    if len(content_bytes) > max_bytes:
        raise ValueError("HTML content is too large. Max allowed is {} bytes.".format(max_bytes))
    conn = get_conn()
    path = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM template_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise ValueError("模板文件不存在。")
        if subject_template is None:
            subject_template, from_name = row["subject_template"], row["from_name"]
        folder = Path(row["file_path"]).parent
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + "_" + row["filename"])
        path.write_bytes(content_bytes)
        has_unsub = int("unsubscribe" in content.lower() or "退订" in content)
        conn.execute("""
            UPDATE template_files
            SET file_path=?, subject_template=?, from_name=?, has_unsubscribe=?
            WHERE id=?
        """, (str(path), subject_template, from_name, has_unsub, file_id))
        conn.commit()
        return {"ok": True, "id": file_id, "bytes": len(content_bytes), "file_path": str(path)}
    except Exception:
        conn.rollback()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()



POOL_0_3 = "warmup_0_3"
POOL_4_30 = "warmup_4_30"
POOL_UNIFIED = "unified"
# Old values remain valid in historical rows and schedules. New uploads only
# create POOL_UNIFIED rows; the UI presents all three as one logical pool.
POOL_TYPES = (POOL_UNIFIED,)
POOL_NAMES = {
    POOL_UNIFIED: "收件人池",
    POOL_0_3: "0-3天库（第1-3天）",
    POOL_4_30: "4-30天库（第4-30天）",
}


def _pool_name(pool_type):
    return POOL_NAMES.get(pool_type, pool_type or "未知库")


def _normalize_pool_type(pool_type):
    value = (pool_type or "").strip()
    if value not in ("", POOL_UNIFIED, POOL_0_3, POOL_4_30, "warmup_named"):
        raise ValueError("Unknown recipient pool type")
    return POOL_UNIFIED


def import_recipient_pool(tag_id, pool_type, source_name, content_bytes):
    """Import into the single visible pool, checking all historical pool types.

    Keep the pool_type argument for old internal callers; it never selects the
    destination. A write transaction prevents concurrent imports from adding
    the same normalized address under different physical pool types.
    """
    pool_type = _normalize_pool_type(pool_type)
    tag_id = int(tag_id)
    text = content_bytes.decode("utf-8-sig", errors="ignore")
    source_name = (source_name or "").strip() or _pool_name(pool_type)
    rows = []
    seen = set()
    invalid = 0
    repeated = 0
    for line in text.splitlines():
        parsed = parse_recipient_line(line)
        if not parsed:
            if line.strip() and not line.strip().startswith("#"):
                invalid += 1
            continue
        email, name = parsed
        email_key = email.strip().lower()
        if email_key in seen:
            repeated += 1
            continue
        seen.add(email_key)
        rows.append((email_key, name))

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone():
            raise ValueError("Tag not found")
        stamp = now_iso()
        imported = 0
        for email_key, name in rows:
            existing = conn.execute("""
                SELECT 1 FROM recipient_pool
                WHERE tag_id=? AND lower(trim(email))=? LIMIT 1
            """, (tag_id, email_key)).fetchone()
            if existing:
                repeated += 1
                continue
            conn.execute("""
                INSERT INTO recipient_pool
                    (tag_id,email,name,pool_type,status,source_name,created_at,updated_at)
                VALUES (?,?,?,?,'available',?,?,?)
            """, (tag_id, email_key, name, POOL_UNIFIED, source_name, stamp, stamp))
            imported += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "parsed": len(rows),
        "imported": imported,
        "duplicates": repeated,
        "invalid": invalid,
        "pool_type": POOL_UNIFIED,
    }


def get_recipient_pool_stats():
    """Return one logical pool per tag, counting each normalized email once."""
    rows = q_all("""
        WITH address_flags AS (
            SELECT tag_id, lower(trim(email)) AS email_key,
                MAX(p.status='reserved') AS reserved,
                MAX(p.status='sent') AS sent,
                MAX(p.status='failed') AS failed,
                MAX(p.status='available' AND (p.pool_type<>'warmup_named' OR EXISTS (
                    SELECT 1 FROM recipient_pool_list_members lm
                    JOIN recipient_lists l ON l.id=lm.list_id
                    WHERE lm.pool_id=p.id AND l.tag_id=p.tag_id AND l.status='active'
                      AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
                ))) AS usable,
                MAX(p.created_at) AS last_import_at
            FROM recipient_pool p
            GROUP BY p.tag_id, lower(trim(p.email))
        ), addresses AS (
            SELECT *, CASE
                WHEN reserved THEN 'reserved'
                WHEN sent THEN 'sent'
                WHEN failed THEN 'failed'
                WHEN usable THEN 'available'
                ELSE 'unavailable' END AS logical_status
            FROM address_flags
        )
        SELECT t.id AS tag_id, t.name AS tag_name,
            COUNT(a.email_key) AS total_count,
            SUM(a.logical_status='available') AS available_count,
            SUM(a.logical_status='reserved') AS reserved_count,
            SUM(a.logical_status='sent') AS sent_count,
            SUM(a.logical_status='failed') AS failed_count,
            SUM(a.logical_status='unavailable') AS unavailable_count,
            MAX(a.last_import_at) AS last_import_at
        FROM tags t
        LEFT JOIN addresses a ON a.tag_id=t.id
        GROUP BY t.id
        ORDER BY t.id DESC
    """)
    for row in rows:
        row["pool_type"] = POOL_UNIFIED
        row["pool_name"] = _pool_name(POOL_UNIFIED)
        for key in ("total_count", "available_count", "reserved_count", "sent_count", "failed_count", "unavailable_count"):
            row[key] = int(row.get(key) or 0)
    return rows


def get_recipient_pool_rows(tag_id, pool_type=None, status="", search="", page=1, page_size=50):
    """Return a paginated, deduplicated view of one tag's logical pool."""
    tag_id = int(tag_id)
    pool_type = _normalize_pool_type(pool_type)
    if not q_one("SELECT id FROM tags WHERE id=?", (tag_id,)):
        raise ValueError("标签不存在")

    allowed_statuses = {"available", "reserved", "sent", "failed", "unavailable"}
    status = (status or "").strip().lower()
    if status and status not in allowed_statuses:
        raise ValueError("未知收件人状态")

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(200, max(10, int(page_size)))
    except (TypeError, ValueError):
        page_size = 50

    conditions = []
    params = []
    if status:
        conditions.append("v.logical_status=?")
        params.append(status)
    search = (search or "").strip()
    if search:
        like = "%{}%".format(search.replace("%", "\\%").replace("_", "\\_"))
        conditions.append("""EXISTS (
            SELECT 1 FROM recipient_pool match_row
            WHERE match_row.tag_id=v.tag_id
              AND lower(trim(match_row.email))=lower(trim(v.email))
              AND (match_row.email LIKE ? ESCAPE '\\'
                OR COALESCE(match_row.name,'') LIKE ? ESCAPE '\\'
                OR COALESCE(match_row.source_name,'') LIKE ? ESCAPE '\\')
        )""")
        params.extend([like, like, like])

    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    base_cte = """
        WITH flags AS (
            SELECT lower(trim(p.email)) AS email_key,
                COUNT(*) AS physical_count,
                MAX(p.status='reserved') AS reserved,
                MAX(p.status='sent') AS sent,
                MAX(p.status='failed') AS failed,
                MAX(p.status='available' AND (p.pool_type<>'warmup_named' OR EXISTS (
                    SELECT 1 FROM recipient_pool_list_members lm
                    JOIN recipient_lists l ON l.id=lm.list_id
                    WHERE lm.pool_id=p.id AND l.tag_id=p.tag_id AND l.status='active'
                      AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
                ))) AS usable
            FROM recipient_pool p WHERE p.tag_id=? GROUP BY lower(trim(p.email))
        ), logical AS (
            SELECT *, CASE
                WHEN reserved THEN 'reserved'
                WHEN sent THEN 'sent'
                WHEN failed THEN 'failed'
                WHEN usable THEN 'available'
                ELSE 'unavailable' END AS logical_status
            FROM flags
        ), ranked AS (
            SELECT p.*, ROW_NUMBER() OVER (
                PARTITION BY lower(trim(p.email))
                ORDER BY CASE p.status
                    WHEN 'reserved' THEN 0 WHEN 'sent' THEN 1
                    WHEN 'failed' THEN 2 ELSE 3 END, p.id
            ) AS row_rank
            FROM recipient_pool p WHERE p.tag_id=?
        ), visible AS (
            SELECT p.*, g.logical_status, g.physical_count
            FROM ranked p JOIN logical g ON g.email_key=lower(trim(p.email))
            WHERE p.row_rank=1
        )
    """
    query_params = (tag_id, tag_id, *params)
    total = _count(base_cte + " SELECT COUNT(*) AS c FROM visible v " + where_sql, query_params)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    offset = (page - 1) * page_size
    rows = q_all(base_cte + """
        SELECT v.*, t.name AS tag_name,
            (SELECT COUNT(*) FROM scheduled_email_tasks s
             JOIN recipient_pool rp ON rp.id=s.recipient_pool_id
             WHERE rp.tag_id=v.tag_id AND lower(trim(rp.email))=lower(trim(v.email))) AS schedule_count,
            (SELECT COUNT(*) FROM scheduled_email_tasks s
             JOIN recipient_pool rp ON rp.id=s.recipient_pool_id
             WHERE rp.tag_id=v.tag_id AND lower(trim(rp.email))=lower(trim(v.email))
               AND s.status IN ('pending','sending','needs_review')) AS active_schedule_count,
            (SELECT COUNT(*) FROM recipient_pool_list_members lm
             JOIN recipient_pool rp ON rp.id=lm.pool_id
             WHERE rp.tag_id=v.tag_id AND lower(trim(rp.email))=lower(trim(v.email))) AS membership_count
        FROM visible v JOIN tags t ON t.id=v.tag_id
        {where_sql} ORDER BY v.id DESC LIMIT ? OFFSET ?
    """.format(where_sql=where_sql), (*query_params, page_size, offset))
    for row in rows:
        row["schedule_count"] = int(row.get("schedule_count") or 0)
        row["active_schedule_count"] = int(row.get("active_schedule_count") or 0)
        row["membership_count"] = int(row.get("membership_count") or 0)
        row["physical_pool_type"] = row["pool_type"]
        row["pool_type"] = POOL_UNIFIED
        row["status"] = row.pop("logical_status")
        row["pool_name"] = _pool_name(POOL_UNIFIED)
        row["identity_editable"] = bool(
            row["status"] == "available" and row["schedule_count"] == 0
            and row["membership_count"] == 0 and row["physical_count"] == 1
        )
        row["deletable"] = bool(
            row["status"] == "available" and row["schedule_count"] == 0
            and row["membership_count"] == 0
        )

    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "tag_id": tag_id,
        "pool_type": pool_type,
        "pool_name": _pool_name(pool_type),
        "status": status,
        "search": search,
    }


def update_recipient_pool_entry(recipient_id, tag_id, pool_type, email, name="", source_name=""):
    """Edit a logical recipient while retaining historical pool references."""
    recipient_id = int(recipient_id)
    tag_id = int(tag_id)
    _normalize_pool_type(pool_type)  # compatibility only; callers cannot move pools
    email = (email or "").strip().lower()
    name = (name or "").strip()
    source_name = (source_name or "").strip()
    if not is_valid_email(email):
        raise ValueError("邮箱格式不正确")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone():
            raise ValueError("标签不存在")
        row = conn.execute("SELECT * FROM recipient_pool WHERE id=?", (recipient_id,)).fetchone()
        if not row:
            raise ValueError("收件人不存在")
        old_tag_id = int(row["tag_id"])
        old_email = (row["email"] or "").strip().lower()
        schedule_count = int(conn.execute(
            """SELECT COUNT(*) FROM scheduled_email_tasks s
               JOIN recipient_pool p ON p.id=s.recipient_pool_id
               WHERE p.tag_id=? AND lower(trim(p.email))=?""",
            (old_tag_id, old_email),
        ).fetchone()[0])
        membership_count = int(conn.execute(
            """SELECT COUNT(*) FROM recipient_pool_list_members lm
               JOIN recipient_pool p ON p.id=lm.pool_id
               WHERE p.tag_id=? AND lower(trim(p.email))=?""",
            (old_tag_id, old_email),
        ).fetchone()[0])
        duplicate_count = int(conn.execute(
            "SELECT COUNT(*) FROM recipient_pool WHERE tag_id=? AND lower(trim(email))=?",
            (old_tag_id, old_email),
        ).fetchone()[0])
        identity_changed = (
            old_tag_id != tag_id or old_email != email
        )
        if identity_changed:
            if (row["status"] != "available" or schedule_count or membership_count
                    or duplicate_count != 1):
                raise ValueError("该邮箱有发送记录、名单关联或历史重复行，只能修改姓名和来源备注。")
            if conn.execute("""
                SELECT 1 FROM send_log l
                LEFT JOIN mail_tasks m ON m.id=l.task_id
                LEFT JOIN send_channels c ON c.id=l.channel_id
                WHERE lower(trim(l.recipient_email))=? AND l.status='sent'
                  AND (COALESCE(m.tag_id,c.tag_id)=? OR (m.tag_id IS NULL AND c.tag_id IS NULL))
                LIMIT 1
            """, (old_email, old_tag_id)).fetchone():
                raise ValueError("该邮箱已有发送审计记录，不能修改地址或标签。")
            if conn.execute("""
                SELECT 1 FROM recipient_pool WHERE tag_id=? AND lower(trim(email))=? LIMIT 1
            """, (tag_id, email)).fetchone():
                raise ValueError("目标标签的统一池中已存在该邮箱。")
        try:
            if identity_changed:
                conn.execute("""
                    UPDATE recipient_pool
                    SET tag_id=?, pool_type=?, email=?, name=?, source_name=?, updated_at=?
                    WHERE id=?
                """, (tag_id, POOL_UNIFIED, email, name, source_name, now_iso(), recipient_id))
            else:
                conn.execute("""
                    UPDATE recipient_pool SET name=?, source_name=?, updated_at=? WHERE id=?
                """, (name, source_name, now_iso(), recipient_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("目标标签的统一池中已存在该邮箱。") from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "id": recipient_id}


def delete_recipient_pool_entry(recipient_id):
    """Delete all unused physical copies of one logical address atomically."""
    recipient_id = int(recipient_id)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id, tag_id, email FROM recipient_pool WHERE id=?", (recipient_id,)).fetchone()
        if not row:
            raise ValueError("收件人不存在")
        tag_id = int(row["tag_id"])
        email_key = row["email"].strip().lower()
        blocked = conn.execute("""
            SELECT 1 FROM recipient_pool p
            WHERE p.tag_id=? AND lower(trim(p.email))=?
              AND (p.status!='available'
                   OR EXISTS (SELECT 1 FROM scheduled_email_tasks s WHERE s.recipient_pool_id=p.id)
                   OR EXISTS (SELECT 1 FROM recipient_pool_list_members lm WHERE lm.pool_id=p.id))
            LIMIT 1
        """, (tag_id, email_key)).fetchone()
        if blocked:
            raise ValueError("该邮箱有计划、发送状态或名单关联，不能删除。")
        if conn.execute("""
            SELECT 1 FROM send_log l
            LEFT JOIN mail_tasks m ON m.id=l.task_id
            LEFT JOIN send_channels c ON c.id=l.channel_id
            WHERE lower(trim(l.recipient_email))=? AND l.status='sent'
              AND (COALESCE(m.tag_id,c.tag_id)=? OR (m.tag_id IS NULL AND c.tag_id IS NULL))
            LIMIT 1
        """, (email_key, tag_id)).fetchone():
            raise ValueError("该邮箱已有发送审计记录，不能删除。")
        deleted = conn.execute(
            "DELETE FROM recipient_pool WHERE tag_id=? AND lower(trim(email))=? AND status='available'",
            (tag_id, email_key),
        ).rowcount
        if not deleted:
            raise ValueError("收件人状态已变化，请刷新后重试")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "id": recipient_id, "email": row["email"], "deleted": deleted}


def delete_available_recipient_pool(tag_id, pool_type=None):
    """Clear unused logical recipients without deleting task or list history."""
    tag_id = int(tag_id)
    _normalize_pool_type(pool_type)
    if not q_one("SELECT id FROM tags WHERE id=?", (tag_id,)):
        raise ValueError("标签不存在")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            """
            DELETE FROM recipient_pool
            WHERE tag_id=? AND status='available'
              AND NOT EXISTS (
                SELECT 1 FROM scheduled_email_tasks s
                WHERE s.recipient_pool_id=recipient_pool.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM recipient_pool_list_members lm
                WHERE lm.pool_id=recipient_pool.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM recipient_pool other
                WHERE other.tag_id=recipient_pool.tag_id
                  AND lower(trim(other.email))=lower(trim(recipient_pool.email))
                  AND other.status!='available'
              )
              AND NOT EXISTS (
                SELECT 1 FROM send_log l
                LEFT JOIN mail_tasks m ON m.id=l.task_id
                LEFT JOIN send_channels c ON c.id=l.channel_id
                WHERE lower(trim(l.recipient_email))=lower(trim(recipient_pool.email))
                  AND l.status='sent'
                  AND (COALESCE(m.tag_id,c.tag_id)=recipient_pool.tag_id
                       OR (m.tag_id IS NULL AND c.tag_id IS NULL))
              )
            """,
            (tag_id,),
        ).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "deleted": int(deleted or 0)}


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
    """Release a task's non-sending reservations atomically.

    Active sending rows are deliberately excluded; callers must reject deletion or
    regeneration while a send is in flight.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sending = conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=? AND status IN ('sending','needs_review')",
            (task_id,),
        ).fetchone()["c"]
        if sending:
            raise ValueError("任务有发送中或待核销邮件，请先处理。")
        stamp = now_iso()
        conn.execute("""
            UPDATE recipient_pool
            SET status='available', reserved_task_id=NULL, reserved_schedule_id=NULL,
                reserved_at=NULL, updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='pending' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, task_id))
        conn.execute("""
            UPDATE recipient_pool
            SET status='failed', reserved_task_id=NULL, reserved_schedule_id=NULL, updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='failed' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, task_id))
        conn.execute("""
            UPDATE recipient_pool
            SET status='sent', reserved_task_id=NULL, reserved_schedule_id=NULL,
                sent_at=COALESCE(sent_at, ?), updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='sent' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, stamp, task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generate_plan(task_id, force=False):
    """Create a capacity-aware 30-day plan in one SQLite write transaction."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        task_row = cur.execute("SELECT * FROM mail_tasks WHERE id=?", (task_id,)).fetchone()
        if not task_row:
            raise ValueError("Task not found")
        task = dict(task_row)
        if task.get("task_kind") == "warmup":
            raise ValueError("预热任务只能从预热系统启动，不能使用旧版计划生成入口。")

        status_counts = {
            row["status"]: int(row["c"])
            for row in cur.execute(
                "SELECT status, COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=? GROUP BY status",
                (task_id,),
            ).fetchall()
        }
        existing = sum(status_counts.values())
        if existing and not force:
            conn.rollback()
            return {"ok": False, "message": "Plan already exists. Use force to recreate.", "existing": existing}
        if force and status_counts.get("sent", 0) > 0:
            raise ValueError("该任务已经有已发送记录，不能直接重生成。请新建任务，避免重复发送。")
        if force and (status_counts.get("sending", 0) > 0 or status_counts.get("needs_review", 0) > 0):
            raise ValueError("任务有发送中或待核销邮件，不能重生成。")
        if force and cur.execute("SELECT 1 FROM send_log WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
            raise ValueError("任务已有发送尝试记录，请保留审计并新建任务。")

        channel_row = cur.execute("SELECT * FROM send_channels WHERE id=?", (task["channel_id"],)).fetchone()
        if not channel_row:
            raise ValueError("Channel not found")
        channel = dict(channel_row)
        group = cur.execute("SELECT tag_id,status FROM template_groups WHERE id=?", (task["template_group_id"],)).fetchone()
        tag = cur.execute("SELECT status FROM tags WHERE id=?", (task["tag_id"],)).fetchone()
        if (channel.get("status") != "active" or channel["tag_id"] != task["tag_id"]
                or not group or group["tag_id"] != task["tag_id"] or group["status"] != "active"
                or not tag or tag["status"] != "active"):
            raise ValueError("标签、通道或模板组不一致或已停用。")

        templates = [dict(row) for row in cur.execute(
            "SELECT * FROM template_files WHERE group_id=? ORDER BY id",
            (task["template_group_id"],),
        ).fetchall()]
        if not templates:
            raise ValueError("No template files in selected group")

        now = datetime.now()
        if now.hour >= 22:
            plan_start = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            plan_start = now

        requested_limits = _build_30_day_warmup_limits()
        daily_limit = max(1, int(channel.get("daily_limit") or 1))
        params = [task["channel_id"]]
        exclude_current = ""
        if force:
            exclude_current = " AND task_id<>?"
            params.append(task_id)
        occupied_rows = cur.execute("""
            SELECT substr(scheduled_at, 1, 10) AS day_key, COUNT(*) AS c
            FROM scheduled_email_tasks
            WHERE channel_id=?
              AND status IN ('pending', 'sending', 'sent')
              {} 
            GROUP BY substr(scheduled_at, 1, 10)
        """.format(exclude_current), tuple(params)).fetchall()
        occupied = {row["day_key"]: int(row["c"]) for row in occupied_rows}
        stat_rows = cur.execute("""
            SELECT date AS day_key, sent_count + reserved_count AS c
            FROM channel_daily_stats
            WHERE channel_id=?
        """, (task["channel_id"],)).fetchall()
        for row in stat_rows:
            # Sent scheduled rows normally appear in both sources. MAX avoids
            # double counting while still covering logs whose task was deleted.
            occupied[row["day_key"]] = max(occupied.get(row["day_key"], 0), int(row["c"] or 0))

        limit_by_day = {}
        for day_offset in range(30):
            day_start, _ = _day_window(plan_start, day_offset)
            remaining = max(0, daily_limit - occupied.get(day_start.date().isoformat(), 0))
            limit_by_day[day_offset] = min(int(requested_limits[day_offset]), remaining)
        if sum(limit_by_day.values()) <= 0:
            raise ValueError("未来 30 天该通道没有可用发送容量，请提高日限额或选择其他通道。")

        # Historical pool_type values remain in the database for existing plans;
        # new plans see one logical, tag-scoped pool keyed by normalized email.
        need_total = sum(limit_by_day.values())
        candidates = cur.execute("""
            SELECT id, email, name, pool_type FROM (
                SELECT p.id,p.email,p.name,p.pool_type,
                    CASE WHEN p.reserved_task_id=? THEN 0 ELSE 1 END AS own_priority,
                    ROW_NUMBER() OVER (
                        PARTITION BY lower(trim(p.email))
                        ORDER BY CASE WHEN p.reserved_task_id=? THEN 0 ELSE 1 END,p.id
                    ) AS email_rank
                FROM recipient_pool p
                WHERE p.tag_id=?
                  AND (p.status='available' OR (p.reserved_task_id=? AND p.status='reserved'))
                  AND (p.pool_type<>'warmup_named' OR EXISTS (
                      SELECT 1 FROM recipient_pool_list_members lm
                      JOIN recipient_lists l ON l.id=lm.list_id
                      WHERE lm.pool_id=p.id AND l.tag_id=p.tag_id AND l.status='active'
                        AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
                  ))
                  AND NOT EXISTS (
                      SELECT 1 FROM scheduled_email_tasks s
                      WHERE s.tag_id=p.tag_id AND lower(trim(s.recipient_email))=lower(trim(p.email))
                        AND s.task_id<>? AND s.status IN ('pending','sending','sent','needs_review')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recipient_pool other_pool
                      WHERE other_pool.tag_id=p.tag_id AND other_pool.id<>p.id
                        AND lower(trim(other_pool.email))=lower(trim(p.email))
                        AND (other_pool.status IN ('sent','failed') OR
                             (other_pool.status='reserved' AND COALESCE(other_pool.reserved_task_id,-1)<>?))
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM send_log l
                      LEFT JOIN mail_tasks other ON other.id=l.task_id
                      LEFT JOIN send_channels sent_channel ON sent_channel.id=l.channel_id
                      WHERE (COALESCE(other.tag_id,sent_channel.tag_id)=p.tag_id
                             OR (other.tag_id IS NULL AND sent_channel.tag_id IS NULL))
                        AND lower(trim(l.recipient_email))=lower(trim(p.email))
                        AND l.status='sent'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM sendgrid_events e
                      WHERE lower(trim(e.email))=lower(trim(p.email))
                        AND lower(e.event_type) IN ('unsubscribe','group_unsubscribe','spamreport','spam report','bounce')
                  )
            ) WHERE email_rank=1 ORDER BY own_priority,id LIMIT ?
        """, (task_id, task_id, task["tag_id"], task_id, task_id, task_id, need_total)).fetchall()
        if len(candidates) < need_total:
            raise ValueError("统一收件人池可用且去重后的邮箱不足：需要 {}，实际 {}。".format(
                need_total, len(candidates)
            ))
        selected = []
        index = 0
        for day_offset in range(30):
            for _ in range(int(limit_by_day[day_offset])):
                selected.append((day_offset, dict(candidates[index])))
                index += 1

        rows = []
        stamp = now_iso()
        for day_offset, rec in selected:
            day_start, day_end = _day_window(plan_start, day_offset)
            tfile = random.choice(templates)
            subject_template = tfile.get("subject_template") or task["subject_template"]
            from_name = tfile.get("from_name") or channel.get("from_name") or ""
            c8 = code8()
            variables = {
                "from_mail": channel["from_email"],
                "to_email": rec["email"],
                "code8": c8,
            }
            rows.append((
                task["id"], task["tag_id"], task["channel_id"],
                rec["email"], rec.get("name") or "",
                channel["from_email"], from_name,
                subject_template, render_vars(subject_template, variables),
                tfile["file_path"], c8,
                _random_time(day_start, day_end).isoformat(timespec="seconds"),
                "pending", 0, stamp, rec["id"], rec["pool_type"], None, None,
            ))

        # Destructive replacement starts only after every validation and allocation
        # succeeded. Any later exception rolls the original plan back intact.
        if force:
            cur.execute("""
                UPDATE recipient_pool
                SET status='available', reserved_task_id=NULL, reserved_schedule_id=NULL,
                    reserved_at=NULL, updated_at=?
                WHERE reserved_task_id=? AND status IN ('reserved', 'failed')
            """, (stamp, task_id))
            cur.execute(
                "DELETE FROM scheduled_email_tasks WHERE task_id=? AND status IN ('pending', 'failed')",
                (task_id,),
            )

        pool_ids = [rec["id"] for _, rec in selected]
        reserved = 0
        for offset in range(0, len(pool_ids), 500):
            chunk = pool_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute("""
                UPDATE recipient_pool
                SET status='reserved', reserved_task_id=?, reserved_schedule_id=NULL,
                    reserved_at=?, updated_at=?
                WHERE id IN ({})
                  AND (status='available' OR reserved_task_id=?)
            """.format(placeholders), (task_id, stamp, stamp, *chunk, task_id))
            reserved += cur.rowcount
        if reserved != len(pool_ids):
            raise RuntimeError("Recipient reservation conflict: expected {}, reserved {}".format(len(pool_ids), reserved))

        cur.executemany("""
            INSERT INTO scheduled_email_tasks (
                task_id, tag_id, channel_id, recipient_email, recipient_name,
                from_email, from_name, subject_template, subject_rendered,
                html_file, code8, scheduled_at, status, attempts, created_at,
                recipient_pool_id, recipient_pool_type, claimed_at, worker_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        cur.execute("UPDATE mail_tasks SET status='planned', updated_at=? WHERE id=?", (stamp, task_id))
        conn.commit()

        return {
            "ok": True,
            "created": len(rows),
            "need_total": need_total,
            "daily_limits": limit_by_day,
            "requested_daily_limits": requested_limits,
            "channel_daily_limit": daily_limit,
            "capacity_capped": any(limit_by_day[d] < requested_limits[d] for d in range(30)),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_task(task_id):
    changed = execute_rowcount("""
        UPDATE mail_tasks SET status='running', updated_at=?
        WHERE id=? AND task_kind='legacy' AND status IN ('planned','paused')
    """, (now_iso(), task_id))
    if changed != 1:
        raise ValueError("历史任务尚未生成计划或状态不允许启动。")


def pause_task(task_id):
    changed = execute_rowcount("""
        UPDATE mail_tasks SET status='paused', updated_at=?
        WHERE id=? AND task_kind='legacy' AND status='running'
    """, (now_iso(), task_id))
    if changed != 1:
        raise ValueError("该历史任务当前不能暂停。")


def resume_task(task_id):
    changed = execute_rowcount("""
        UPDATE mail_tasks SET status='running', updated_at=?
        WHERE id=? AND task_kind='legacy' AND status='paused'
    """, (now_iso(), task_id))
    if changed != 1:
        raise ValueError("该历史任务当前不能继续。")


def delete_mail_task(task_id):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        kind = cur.execute("SELECT task_kind FROM mail_tasks WHERE id=?", (task_id,)).fetchone()
        if not kind or kind["task_kind"] != "legacy":
            raise ValueError("预热任务请从预热系统管理。")
        sending = cur.execute(
            "SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=? AND status IN ('sending','needs_review','sent')",
            (task_id,),
        ).fetchone()["c"]
        logged = cur.execute("SELECT 1 FROM send_log WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
        if sending or logged:
            raise ValueError("任务有发送或核销记录，不能删除历史。")
        stamp = now_iso()
        cur.execute("""
            UPDATE recipient_pool
            SET status='available', reserved_task_id=NULL, reserved_schedule_id=NULL,
                reserved_at=NULL, updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='pending' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, task_id))
        cur.execute("""
            UPDATE recipient_pool
            SET status='failed', reserved_task_id=NULL, reserved_schedule_id=NULL, updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='failed' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, task_id))
        cur.execute("""
            UPDATE recipient_pool
            SET status='sent', reserved_task_id=NULL, reserved_schedule_id=NULL,
                sent_at=COALESCE(sent_at, ?), updated_at=?
            WHERE id IN (
                SELECT recipient_pool_id FROM scheduled_email_tasks
                WHERE task_id=? AND status='sent' AND recipient_pool_id IS NOT NULL
            )
        """, (stamp, stamp, task_id))
        cur.execute("DELETE FROM scheduled_email_tasks WHERE task_id=?", (task_id,))
        cur.execute("DELETE FROM mail_task_recipient_lists WHERE task_id=?", (task_id,))
        cur.execute("DELETE FROM mail_tasks WHERE id=?", (task_id,))
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
            SUM(CASE WHEN s.status='needs_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN s.status='sending' THEN 1 ELSE 0 END) AS sending_count,
            SUM(CASE WHEN s.status='missed' THEN 1 ELSE 0 END) AS missed_count,
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
        row["review_count"] = int(row.get("review_count") or 0)
        row["sending_count"] = int(row.get("sending_count") or 0)
        row["missed_count"] = int(row.get("missed_count") or 0)
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
                "review_count": 0,
                "sending_count": 0,
                "missed_count": 0,
                "plans": [],
            }
        tag_group = tag_map[tag_id]
        tag_group["total_count"] += row["total_count"]
        tag_group["pending_count"] += row["pending_count"]
        tag_group["sent_count"] += row["sent_count"]
        tag_group["failed_count"] += row["failed_count"]
        tag_group["review_count"] += row["review_count"]
        tag_group["sending_count"] += row["sending_count"]
        tag_group["missed_count"] += row["missed_count"]
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
            SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END) AS sending_count,
            SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed_count,
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
        row["review_count"] = int(row.get("review_count") or 0)
        row["sending_count"] = int(row.get("sending_count") or 0)
        row["missed_count"] = int(row.get("missed_count") or 0)
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
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=?) AS logs_total,
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=? AND l.status='sent') AS logs_sent,
            (SELECT COUNT(*) FROM send_log l LEFT JOIN mail_tasks m ON m.id=l.task_id LEFT JOIN send_channels c ON c.id=l.channel_id WHERE COALESCE(m.tag_id, c.tag_id)=? AND l.status='failed') AS logs_failed
    """, (tag_id,) * 13) or {}
    _to_int_fields(counters, [
        "channels_total", "channels_active", "template_groups_total", "template_files_total",
        "tasks_total", "tasks_running", "scheduled_total", "scheduled_pending",
        "scheduled_sent", "scheduled_failed", "logs_total", "logs_sent", "logs_failed"
    ])

    pool_summary = next(
        (row for row in get_recipient_pool_stats() if int(row["tag_id"]) == tag_id),
        {"pool_type": POOL_UNIFIED, "pool_name": _pool_name(POOL_UNIFIED),
         "total_count": 0, "available_count": 0, "reserved_count": 0,
         "sent_count": 0, "failed_count": 0, "last_import_at": None},
    )
    pool_summary["last_updated_at"] = q_one(
        "SELECT MAX(updated_at) AS value FROM recipient_pool WHERE tag_id=?", (tag_id,)
    )["value"]
    counters.update({
        "pool_total": pool_summary["total_count"],
        "pool_available": pool_summary["available_count"],
        "pool_reserved": pool_summary["reserved_count"],
        "pool_sent": pool_summary["sent_count"],
        "pool_failed": pool_summary["failed_count"],
    })
    pools = [pool_summary]

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
            m.id, m.name, m.task_kind, m.subject_template, m.status, m.created_at, m.updated_at,
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
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END) AS sending_count,
            SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed_count
        FROM scheduled_email_tasks
        WHERE tag_id=?
        GROUP BY substr(scheduled_at, 1, 10)
        ORDER BY date ASC
        LIMIT 60
    """, (tag_id,))
    for row in schedule_by_date:
        _to_int_fields(row, ["total_count", "pending_count", "sent_count", "failed_count",
                             "review_count", "sending_count", "missed_count"])

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
                   '统一收件人池' AS recipient_list_name,
                   1 AS recipient_list_count,
                   (SELECT COUNT(*) FROM (
                       SELECT lower(trim(p.email)) AS email_key
                       FROM recipient_pool p WHERE p.tag_id=m.tag_id
                       GROUP BY lower(trim(p.email))
                       HAVING MAX(p.status='reserved')=0
                          AND MAX(p.status='sent')=0
                          AND MAX(p.status='failed')=0
                   )) AS recipient_count,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id) AS scheduled_count,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id AND s.status='sent') AS sent_count,
                   (SELECT COUNT(*) FROM scheduled_email_tasks s WHERE s.task_id=m.id AND s.status='failed') AS failed_count
            FROM mail_tasks m
            LEFT JOIN tags t ON t.id=m.tag_id
            LEFT JOIN send_channels c ON c.id=m.channel_id
            WHERE m.task_kind='legacy'
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
        "legacy_reviews": q_all("""
            SELECT s.id AS schedule_id,s.task_id,s.recipient_email,s.claimed_at,
                   m.name AS task_name
            FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            WHERE m.task_kind='legacy' AND s.status='needs_review'
            ORDER BY s.claimed_at ASC,s.id ASC
            LIMIT 200
        """),
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
            "total_review": _count("SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE status='needs_review'"),
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


def _reserve_channel_slot(channel_id, daily_limit, scheduled_id, worker_id):
    """Atomically reserve daily capacity and attach it to the claimed message."""
    slot_date = today()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO channel_daily_stats
                (channel_id, date, sent_count, failed_count, reserved_count, last_error)
            VALUES (?, ?, 0, 0, 0, NULL)
        """, (channel_id, slot_date))
        cur.execute("""
            UPDATE channel_daily_stats
            SET reserved_count=reserved_count + 1
            WHERE channel_id=? AND date=?
              AND sent_count + reserved_count < ?
        """, (channel_id, slot_date, max(1, int(daily_limit))))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        cur.execute("""
            UPDATE scheduled_email_tasks
            SET channel_slot_date=?
            WHERE id=? AND status='sending' AND worker_id=?
        """, (slot_date, scheduled_id, worker_id))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return slot_date
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _complete_channel_slot(channel_id, slot_date, sent_ok, error=None):
    if not slot_date:
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if sent_ok:
            conn.execute("""
                UPDATE channel_daily_stats
                SET reserved_count=MAX(0, reserved_count - 1), sent_count=sent_count + 1
                WHERE channel_id=? AND date=?
            """, (channel_id, slot_date))
        else:
            conn.execute("""
                UPDATE channel_daily_stats
                SET reserved_count=MAX(0, reserved_count - 1),
                    failed_count=failed_count + 1, last_error=?
                WHERE channel_id=? AND date=?
            """, (error, channel_id, slot_date))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def increment_channel_stat(channel_id, sent_ok, error=None):
    """Backward-compatible wrapper for callers outside the worker path."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT OR IGNORE INTO channel_daily_stats
                (channel_id, date, sent_count, failed_count, reserved_count, last_error)
            VALUES (?, ?, 0, 0, 0, NULL)
        """, (channel_id, today()))
        if sent_ok:
            conn.execute("""
                UPDATE channel_daily_stats SET sent_count=sent_count + 1
                WHERE channel_id=? AND date=?
            """, (channel_id, today()))
        else:
            conn.execute("""
                UPDATE channel_daily_stats
                SET failed_count=failed_count + 1, last_error=?
                WHERE channel_id=? AND date=?
            """, (error, channel_id, today()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_proxies(channel):
    if not channel.get("proxy_id"):
        return None
    proxy = q_one("SELECT * FROM proxies WHERE id=? AND status='active'", (channel["proxy_id"],))
    if not proxy:
        raise ValueError("Configured proxy is missing or inactive")
    proxy_url = normalize_proxy_url(unprotect(proxy["proxy_url_protected"]))
    if not proxy_url:
        raise ValueError("Configured proxy URL is invalid")
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
        return False, None, "Channel not found", "NOT_SENT"

    request_started = False
    try:
        task_group = q_one("""
            SELECT m.tag_id AS task_tag,g.tag_id AS group_tag,g.status AS group_status,
                   t.status AS tag_status
            FROM mail_tasks m JOIN template_groups g ON g.id=m.template_group_id
            JOIN tags t ON t.id=m.tag_id WHERE m.id=?
        """, (scheduled["task_id"],))
        if (channel["status"] != "active" or channel["tag_id"] != scheduled["tag_id"]
                or not task_group or task_group["task_tag"] != scheduled["tag_id"]
                or task_group["group_tag"] != scheduled["tag_id"]
                or task_group["group_status"] != "active" or task_group["tag_status"] != "active"):
            return False, None, "Tag, channel or template is inactive or mismatched", "NOT_SENT"

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
                return False, None, "Marketing HTML does not contain unsubscribe keyword/link", "NOT_SENT"

        payload = {
            "personalizations": [{
                "to": [{"email": scheduled["recipient_email"]}],
                "custom_args": {
                    "source": "web_admin_scheduler",
                    "scheduled_task_id": str(scheduled["id"]),
                    "task_id": str(scheduled["task_id"]),
                    "channel_id": str(channel["id"]),
                    "code8": scheduled["code8"],
                },
            }],
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
        proxies = _build_proxies(channel)
        request_started = True
        resp = requests.post(
            SENDGRID_URL,
            headers=headers,
            json=payload,
            proxies=proxies,
            timeout=settings.request_timeout_seconds,
        )
        msg_id = resp.headers.get("X-Message-Id") or resp.headers.get("x-message-id")
        ok = resp.status_code in (200, 202)
        err = None if ok else resp.text
        request_log = json.dumps(payload, ensure_ascii=False) if settings.store_send_request_body else None
        _log_send_attempt(
            scheduled, channel, subject_rendered, resp.status_code, msg_id,
            "sent" if ok else "failed", err, request_log, (resp.text or "")[:10000],
        )
        return ok, msg_id, err, resp.text
    except Exception as exc:
        error = str(exc)
        _log_send_attempt(
            scheduled, channel, scheduled.get("subject_rendered") or "", None, None,
            "failed", error, None, None,
        )
        # Preflight errors are definitely unsent. A transport/logging exception
        # after starting HTTP might have been accepted, so it needs review.
        return False, None, error, None if request_started else "NOT_SENT"


def _next_day_retry_at():
    return (datetime.now() + timedelta(days=1)).replace(
        hour=8, minute=5, second=0, microsecond=0
    ).isoformat(timespec="seconds")


def _recover_stale_sending_claims():
    settings = get_settings()
    timeout_seconds = max(settings.worker_claim_timeout_seconds, settings.request_timeout_seconds * 2)
    cutoff_epoch = int(time.time()) - timeout_seconds
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        # A provider 202 may already be durably logged when a legacy worker
        # exits before updating the scheduled row. Never submit that row again.
        logged_successes = cur.execute("""
            SELECT s.id,s.task_id,s.channel_id,s.channel_slot_date,s.recipient_pool_id,
                (SELECT MAX(l.created_at) FROM send_log l
                 WHERE l.scheduled_task_id=s.id AND l.status='sent') AS accepted_at
            FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            WHERE m.task_kind='legacy' AND s.status='sending'
              AND (s.claimed_epoch IS NULL OR s.claimed_epoch<=?)
              AND EXISTS (
                  SELECT 1 FROM send_log l
                  WHERE l.scheduled_task_id=s.id AND l.status='sent'
              )
        """, (cutoff_epoch,)).fetchall()
        for row in logged_successes:
            accepted_at = row["accepted_at"] or now_iso()
            cur.execute("""
                UPDATE scheduled_email_tasks
                SET status='sent',sent_at=?,worker_id=NULL,claimed_at=NULL,claimed_epoch=NULL,
                    channel_slot_date=NULL,last_error=NULL
                WHERE id=? AND status='sending'
            """, (accepted_at, row["id"]))
            if row["channel_slot_date"]:
                cur.execute("""
                    UPDATE channel_daily_stats
                    SET reserved_count=reserved_count-1,sent_count=sent_count+1
                    WHERE channel_id=? AND date=? AND reserved_count>0
                """, (row["channel_id"], row["channel_slot_date"]))
            if row["recipient_pool_id"]:
                cur.execute("""
                    UPDATE recipient_pool
                    SET status='sent',sent_at=?,updated_at=?
                    WHERE id=? AND reserved_task_id=?
                """, (accepted_at, accepted_at, row["recipient_pool_id"], row["task_id"]))
        # No durable success record does not prove the request was unsent. Keep
        # channel capacity and recipient reserved until an admin checks SendGrid.
        cur.execute("""
            UPDATE scheduled_email_tasks
            SET status='needs_review', worker_id=NULL,
                last_error=CASE
                    WHEN last_error IS NULL OR last_error='' THEN 'Worker stopped during send; review provider outcome'
                    ELSE last_error || ' | Worker stopped during send; review provider outcome'
                END
            WHERE status='sending' AND (claimed_epoch IS NULL OR claimed_epoch <= ?)
              AND EXISTS (
                  SELECT 1 FROM mail_tasks m
                  WHERE m.id=scheduled_email_tasks.task_id AND m.task_kind='legacy'
              )
        """, (cutoff_epoch,))
        recovered = cur.rowcount
        conn.commit()
        return recovered + len(logged_successes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _legacy_recipient_still_allowed(cur, schedule):
    """Recheck opt-outs and reservation ownership immediately before HTTP."""
    email = (schedule["recipient_email"] or "").strip().lower()
    if not email or cur.execute("""
        SELECT 1 FROM sendgrid_events
        WHERE lower(trim(email))=?
          AND lower(event_type) IN ('unsubscribe','group_unsubscribe','spamreport','spam report','bounce')
        LIMIT 1
    """, (email,)).fetchone():
        return False
    if not schedule.get("recipient_pool_id"):
        return True  # Old plans made before the reusable pool was introduced.
    pool = cur.execute("""
        SELECT p.tag_id,p.email,p.pool_type,p.status,p.reserved_task_id
        FROM recipient_pool p WHERE p.id=?
    """, (schedule["recipient_pool_id"],)).fetchone()
    if (not pool or pool["status"] != "reserved" or pool["reserved_task_id"] != schedule["task_id"]
            or pool["tag_id"] != schedule["tag_id"] or pool["email"].strip().lower() != email):
        return False
    if pool["pool_type"] == "warmup_named" and not cur.execute("""
        SELECT 1 FROM recipient_pool_list_members lm JOIN recipient_lists l ON l.id=lm.list_id
        WHERE lm.pool_id=? AND l.tag_id=? AND l.status='active'
          AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL LIMIT 1
    """, (schedule["recipient_pool_id"], schedule["tag_id"])).fetchone():
        return False
    return True


def _settle_legacy_send(schedule, worker_id, slot_date, ok, raw, error, uncertain):
    """Commit result, daily capacity and recipient state as one transaction."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        current = cur.execute("""
            SELECT * FROM scheduled_email_tasks WHERE id=? AND status='sending' AND worker_id=?
        """, (schedule["id"], worker_id)).fetchone()
        if not current:
            conn.commit()
            return False
        result = "needs_review" if uncertain else ("sent" if ok else "failed")
        stamp = now_iso()
        cur.execute("""
            UPDATE scheduled_email_tasks SET status=?,attempts=attempts+1,
                sent_at=?,sender_response=?,last_error=?,worker_id=NULL,
                claimed_at=?,claimed_epoch=?,channel_slot_date=?
            WHERE id=? AND status='sending' AND worker_id=?
        """, (result, stamp if ok else None, str(raw or "")[:10000], error,
              current["claimed_at"] if uncertain else None,
              current["claimed_epoch"] if uncertain else None,
              slot_date if uncertain else None, schedule["id"], worker_id))
        if not uncertain:
            if slot_date:
                cur.execute("""
                    UPDATE channel_daily_stats
                    SET reserved_count=MAX(0,reserved_count-1),
                        sent_count=sent_count+?,failed_count=failed_count+?,
                        last_error=CASE WHEN ?=1 THEN last_error ELSE ? END
                    WHERE channel_id=? AND date=?
                """, (1 if ok else 0, 0 if ok else 1, 1 if ok else 0,
                      error, schedule["channel_id"], slot_date))
            if schedule.get("recipient_pool_id"):
                if ok:
                    cur.execute("""
                        UPDATE recipient_pool SET status='sent',sent_at=?,updated_at=?
                        WHERE id=? AND reserved_task_id=? AND status='reserved'
                    """, (stamp, stamp, schedule["recipient_pool_id"], schedule["task_id"]))
                else:
                    cur.execute("""
                        UPDATE recipient_pool SET status='failed',reserved_task_id=NULL,
                            reserved_schedule_id=NULL,reserved_at=NULL,updated_at=?
                        WHERE id=? AND reserved_task_id=? AND status='reserved'
                    """, (stamp, schedule["recipient_pool_id"], schedule["task_id"]))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_legacy_review(schedule_id, resolution):
    """Reconcile an uncertain legacy send while preserving its capacity hold."""
    if resolution not in ("accepted", "failed"):
        raise ValueError("核销结果必须为已接受或明确失败。")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        row = cur.execute("""
            SELECT s.* FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            WHERE s.id=? AND m.task_kind='legacy' AND s.status='needs_review'
        """, (schedule_id,)).fetchone()
        if not row:
            raise ValueError("邮件不处于待人工核销状态。")
        accepted = resolution == "accepted"
        stamp = now_iso()
        cur.execute("""
            UPDATE scheduled_email_tasks
            SET status=?,sent_at=?,last_error=?,worker_id=NULL,
                channel_slot_date=NULL,claimed_at=NULL,claimed_epoch=NULL
            WHERE id=? AND status='needs_review'
        """, ("sent" if accepted else "failed", stamp if accepted else None,
              "Manually resolved: " + resolution, schedule_id))
        if row["channel_slot_date"]:
            cur.execute("""
                UPDATE channel_daily_stats
                SET reserved_count=MAX(0,reserved_count-1),
                    sent_count=sent_count+?,failed_count=failed_count+?
                WHERE channel_id=? AND date=?
            """, (1 if accepted else 0, 0 if accepted else 1,
                  row["channel_id"], row["channel_slot_date"]))
        if row["recipient_pool_id"]:
            if accepted:
                cur.execute("""
                    UPDATE recipient_pool SET status='sent',sent_at=?,updated_at=?
                    WHERE id=? AND reserved_task_id=? AND status='reserved'
                """, (stamp, stamp, row["recipient_pool_id"], row["task_id"]))
            else:
                cur.execute("""
                    UPDATE recipient_pool SET status='failed',reserved_task_id=NULL,
                        reserved_schedule_id=NULL,reserved_at=NULL,updated_at=?
                    WHERE id=? AND reserved_task_id=? AND status='reserved'
                """, (stamp, row["recipient_pool_id"], row["task_id"]))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_due_tasks(limit):
    _recover_stale_sending_claims()
    now_value = now_iso()
    due = q_all("""
        SELECT s.*
        FROM scheduled_email_tasks s
        JOIN mail_tasks m ON m.id=s.task_id
        WHERE s.status='pending'
          AND m.status='running'
          AND m.task_kind='legacy'
          AND s.scheduled_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_email_tasks active
              WHERE active.task_id=s.task_id AND active.status IN ('sending','needs_review')
          )
        ORDER BY s.scheduled_at ASC, s.id ASC
        LIMIT ?
    """, (now_value, max(1, int(limit))))

    processed = 0
    worker_id = uuid.uuid4().hex
    for task in due:
        claimed_at = now_iso()
        claimed = execute_rowcount("""
            UPDATE scheduled_email_tasks
            SET status='sending', claimed_at=?, claimed_epoch=?, worker_id=?
            WHERE id=? AND status='pending' AND scheduled_at <= ?
              AND EXISTS (
                  SELECT 1 FROM mail_tasks m
                  WHERE m.id=scheduled_email_tasks.task_id AND m.status='running'
                    AND m.task_kind='legacy'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM scheduled_email_tasks active
                  WHERE active.task_id=scheduled_email_tasks.task_id
                    AND active.status IN ('sending','needs_review')
              )
        """, (claimed_at, int(time.time()), worker_id, task["id"], now_value))
        if claimed != 1:
            continue

        task = q_one("SELECT * FROM scheduled_email_tasks WHERE id=?", (task["id"],)) or task
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            if not _legacy_recipient_still_allowed(cur, task):
                cur.execute("""
                    UPDATE scheduled_email_tasks
                    SET status='missed',last_error='Recipient suppressed or source disabled',
                        worker_id=NULL,claimed_at=NULL,claimed_epoch=NULL
                    WHERE id=? AND status='sending' AND worker_id=?
                """, (task["id"], worker_id))
                if task.get("recipient_pool_id"):
                    cur.execute("""
                        UPDATE recipient_pool SET status='available',reserved_task_id=NULL,
                            reserved_schedule_id=NULL,reserved_at=NULL,updated_at=?
                        WHERE id=? AND reserved_task_id=? AND status='reserved'
                    """, (now_iso(), task["recipient_pool_id"], task["task_id"]))
                conn.commit()
                processed += 1
                continue
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        channel = q_one("SELECT * FROM send_channels WHERE id=?", (task["channel_id"],))
        slot_date = None
        if channel and channel.get("status") == "active" and channel["tag_id"] == task["tag_id"]:
            slot_date = _reserve_channel_slot(
                channel["id"], channel.get("daily_limit") or 1, task["id"], worker_id
            )

        if channel and channel.get("status") == "active" and channel["tag_id"] == task["tag_id"] and not slot_date:
            execute("""
                UPDATE scheduled_email_tasks
                SET status='pending', scheduled_at=?, last_error=?, worker_id=NULL,
                    claimed_at=NULL,claimed_epoch=NULL,channel_slot_date=NULL
                WHERE id=? AND status='sending' AND worker_id=?
            """, (_next_day_retry_at(), "Channel daily limit reached", task["id"], worker_id))
            processed += 1
            continue

        if not channel:
            ok, msg_id, err, raw = False, None, "Channel not found", "NOT_SENT"
        elif channel.get("status") != "active" or channel["tag_id"] != task["tag_id"]:
            ok, msg_id, err, raw = False, None, "Channel inactive or tag mismatch", "NOT_SENT"
        else:
            try:
                ok, msg_id, err, raw = _send_via_sendgrid(task)
            except Exception as exc:
                ok, msg_id, err, raw = False, None, str(exc)[:500], None

        uncertain = not ok and raw is None
        if not ok and not uncertain and raw != "NOT_SENT":
            log = q_one("""
                SELECT http_status FROM send_log WHERE scheduled_task_id=? ORDER BY id DESC LIMIT 1
            """, (task["id"],))
            http_status = log.get("http_status") if log else None
            uncertain = http_status in (408, 429) or (http_status is not None and http_status >= 500)
        _settle_legacy_send(task, worker_id, slot_date, ok, raw or msg_id, err, uncertain)
        processed += 1
    return processed


def record_sendgrid_events(payload):
    if isinstance(payload, dict):
        events = [payload]
    elif isinstance(payload, list):
        events = payload
    else:
        events = []

    conn = get_conn()
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            email = ev.get("email")
            event_type = ev.get("event") or ev.get("event_type") or "unknown"
            timestamp_value = str(ev.get("timestamp") or "")
            sg_message_id = ev.get("sg_message_id") or ev.get("sg_message-id") or ev.get("sendgrid_message_id")
            smtp_id = ev.get("smtp-id") or ev.get("smtp_id")
            reason = ev.get("reason") or ev.get("response") or ev.get("status")
            raw_json = json.dumps(ev, ensure_ascii=False, sort_keys=True)
            provider_event_id = ev.get("sg_event_id") or ev.get("event_id")
            # Signed Event Webhook payloads normally include sg_event_id.
            # For older payloads, hashing canonical raw JSON deduplicates exact
            # retries without collapsing distinct events that share a timestamp.
            identity = str(provider_event_id or raw_json)
            event_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            cur.execute("""
                INSERT OR IGNORE INTO sendgrid_events (
                    email, event_type, timestamp_value, sg_message_id, smtp_id,
                    reason, raw_json, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                email, event_type, timestamp_value, sg_message_id, smtp_id,
                reason, raw_json, event_key, now_iso(),
            ))
            inserted += max(0, cur.rowcount)
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
