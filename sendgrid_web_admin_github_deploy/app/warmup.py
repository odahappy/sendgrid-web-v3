"""User-configured, consent-based warm-up schedules.

Legacy thirty-day plans deliberately remain in services.py. All times in this
module are Unix UTC seconds; scheduled_at is a local display value only.
"""

import random
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .crypto import unprotect
from .db import get_conn, q_all, q_one
from .utils import code8, now_iso, parse_recipient_line, render_vars
from . import services

DAY = 86400
MAX_DAYS = 90
MAX_TOTAL = 100000
UNIFIED_POOL = services.POOL_UNIFIED
POOL_TYPES = (UNIFIED_POOL, services.POOL_0_3, services.POOL_4_30)
NAMED_POOL = "warmup_named"
SUPPRESSION_TYPES = ("unsubscribe", "group_unsubscribe", "spamreport", "spam report", "bounce")


def _int(value, label):
    if isinstance(value, bool) or str(value).strip() == "":
        raise ValueError("{} 必须填写整数。".format(label))
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("{} 必须填写整数。".format(label))
    if str(result) != str(value).strip():
        raise ValueError("{} 必须填写整数。".format(label))
    return result


def _validate_counts(day_counts, interval_mode, interval_seconds):
    if not 1 <= len(day_counts) <= MAX_DAYS:
        raise ValueError("预热天数必须为 1–{} 天。".format(MAX_DAYS))
    counts = []
    for index, raw in enumerate(day_counts):
        value = _int(raw, "第 {} 天数量".format(index + 1))
        if value < 0:
            raise ValueError("每日数量不能为负数。")
        counts.append(value)
    if not 0 < sum(counts) <= MAX_TOTAL:
        raise ValueError("总发送数量必须在 1–{} 封之间。".format(MAX_TOTAL))
    if interval_mode not in ("auto", "manual"):
        raise ValueError("请选择系统自动或手动间隔。")
    seconds = _int(interval_seconds, "手动间隔秒数") if interval_mode == "manual" else None
    if seconds is not None:
        if seconds <= 0 or seconds >= DAY:
            raise ValueError("手动间隔须大于 0 且小于 86400 秒。")
        for index, count in enumerate(counts):
            if (count - 1) * seconds >= DAY:
                raise ValueError("第 {} 天的数量和间隔无法在 24 小时内完成。".format(index + 1))
    elif max(counts) > DAY:
        raise ValueError("自动间隔每天最多安排 86400 封。")
    return counts, seconds


def _resources(cur, tag_id, channel_id, template_group_id):
    tag = cur.execute("SELECT id, status FROM tags WHERE id=?", (tag_id,)).fetchone()
    channel = cur.execute("SELECT * FROM send_channels WHERE id=?", (channel_id,)).fetchone()
    group = cur.execute("SELECT * FROM template_groups WHERE id=?", (template_group_id,)).fetchone()
    if not tag or tag["status"] != "active":
        raise ValueError("标签不存在或已停用。")
    if not channel or channel["tag_id"] != tag_id or channel["status"] != "active":
        raise ValueError("所选通道不属于标签或已停用。")
    if not group or group["tag_id"] != tag_id or group["status"] != "active":
        raise ValueError("所选模板组不属于标签或已停用。")
    templates = cur.execute(
        "SELECT * FROM template_files WHERE group_id=? ORDER BY id",
        (template_group_id,),
    ).fetchall()
    if not templates:
        raise ValueError("所选模板组还没有 HTML 文件。")
    return dict(channel), [dict(row) for row in templates]


def _sources(cur, tag_id, pool_types, list_ids):
    pools = list(dict.fromkeys(str(value) for value in (pool_types or [])))
    lists = list(dict.fromkeys(_int(value, "名单 ID") for value in (list_ids or [])))
    if not pools and not lists:
        raise ValueError("请至少选择一个收件人来源。")
    if any(value not in POOL_TYPES for value in pools):
        raise ValueError("未知收件人池。")
    if UNIFIED_POOL in pools and (len(pools) != 1 or lists):
        raise ValueError("全部收件人和指定名单不能同时选择。")
    for list_id in lists:
        row = cur.execute(
            "SELECT tag_id, status, consent_source, consented_at, list_group FROM recipient_lists WHERE id=?",
            (list_id,),
        ).fetchone()
        if (not row or row["tag_id"] != tag_id or row["status"] != "active"
                or row["list_group"] != NAMED_POOL or not row["consent_source"]
                or not row["consented_at"]):
            raise ValueError("具名名单未授权、已停用或不属于当前标签。")
    return pools, lists


def create_task(tag_id, channel_id, name, subject_template, template_group_id,
                day_counts, interval_mode, interval_seconds, source_pool_types,
                source_list_ids, consent_confirmed):
    counts, seconds = _validate_counts(day_counts, interval_mode, interval_seconds)
    if str(consent_confirmed).lower() not in ("1", "true", "yes", "on"):
        raise ValueError("请确认所选地址已同意接收对应邮件。")
    name, subject_template = (name or "").strip(), (subject_template or "").strip()
    if not name or len(name) > 180 or len(subject_template) > 500:
        raise ValueError("请填写有效的任务名称与邮件主题。")
    tag_id, channel_id, template_group_id = int(tag_id), int(channel_id), int(template_group_id)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        _, templates = _resources(cur, tag_id, channel_id, template_group_id)
        if not subject_template and any(not template.get("subject_template") for template in templates):
            raise ValueError("所选模板有未绑定主题的旧文件，请填写任务默认主题。")
        pools, lists = _sources(cur, tag_id, source_pool_types, source_list_ids)
        stamp = now_iso()
        cur.execute("""
            INSERT INTO mail_tasks (
                tag_id, channel_id, name, subject_template, template_group_id,
                recipient_list_id, batch1_list_id, batch2_list_id,
                status, task_kind, warmup_days, warmup_interval_mode,
                warmup_interval_seconds, warmup_consent_confirmed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 'draft', 'warmup', ?, ?, ?, 1, ?, ?)
        """, (tag_id, channel_id, name, subject_template, template_group_id,
              len(counts), interval_mode, seconds, stamp, stamp))
        task_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO warmup_day_plans (task_id, day_index, quota) VALUES (?, ?, ?)",
            [(task_id, index, count) for index, count in enumerate(counts)],
        )
        cur.executemany(
            "INSERT INTO warmup_task_sources (task_id, source_type, source_id) VALUES (?, ?, ?)",
            [(task_id, "pool", value) for value in pools]
            + [(task_id, "list", str(value)) for value in lists],
        )
        conn.commit()
        return task_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_named_list(tag_id, name, consent_source, consented_at, content_bytes):
    """Add membership without creating a second copy of an existing address."""
    tag_id, name = int(tag_id), (name or "").strip()
    consent_source, consented_at = (consent_source or "").strip(), (consented_at or "").strip()
    if not name or len(name) > 180 or not consent_source or len(consent_source) > 500:
        raise ValueError("名单名称和授权来源均为必填项。")
    try:
        datetime.fromisoformat(consented_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("请填写有效的授权日期时间。")
    if not content_bytes or len(content_bytes) > get_settings().max_recipient_upload_bytes * get_settings().max_recipient_files_per_upload:
        raise ValueError("名单文件为空或超出上传上限。")
    rows = {}
    invalid = 0
    for line in content_bytes.decode("utf-8-sig", errors="ignore").splitlines():
        parsed = parse_recipient_line(line)
        if parsed:
            email, person = parsed
            rows.setdefault(email.strip().lower(), person)
        elif line.strip() and not line.strip().startswith("#"):
            invalid += 1
    if not rows:
        raise ValueError("名单中没有有效的邮箱地址。")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        tag = cur.execute("SELECT id FROM tags WHERE id=? AND status='active'", (tag_id,)).fetchone()
        if not tag:
            raise ValueError("标签不存在或已停用。")
        stamp = now_iso()
        cur.execute("""
            INSERT INTO recipient_lists
                (tag_id, name, list_group, status, consent_source, consented_at, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
        """, (tag_id, name, NAMED_POOL, consent_source, consented_at, stamp, stamp))
        list_id = cur.lastrowid
        for email, person in rows.items():
            # Historical databases may contain the same address in several old
            # pools. Prefer a used row so importing a list never revives an
            # address that has already been reserved or sent.
            pool = cur.execute("""
                SELECT id FROM recipient_pool
                WHERE tag_id=? AND lower(trim(email))=?
                ORDER BY CASE status WHEN 'sent' THEN 0 WHEN 'reserved' THEN 1
                                     WHEN 'failed' THEN 2 ELSE 3 END, id
                LIMIT 1
            """, (tag_id, email)).fetchone()
            if not pool:
                # This physical type records that the address has no consent
                # source outside its named list. It remains part of the one
                # logical pool, but disabling the list must remove eligibility.
                cur.execute("""
                    INSERT INTO recipient_pool
                        (tag_id, email, name, pool_type, status, source_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'available', ?, ?, ?)
                """, (tag_id, email, person, NAMED_POOL, name, stamp, stamp))
                pool = {"id": cur.lastrowid}
            cur.execute(
                "INSERT OR IGNORE INTO recipient_pool_list_members (list_id, pool_id) VALUES (?, ?)",
                (list_id, pool["id"]),
            )
        conn.commit()
        return {"id": list_id, "parsed": len(rows), "invalid": invalid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def disable_named_list(list_id):
    conn = get_conn()
    try:
        cur = conn.execute("""
            UPDATE recipient_lists SET status='disabled',updated_at=?
            WHERE id=? AND list_group=? AND status='active'
        """, (now_iso(), list_id, NAMED_POOL))
        conn.commit()
        if cur.rowcount != 1:
            raise ValueError("具名名单不存在或已停用。")
        return True
    finally:
        conn.close()


def _suppressed(cur, email):
    return cur.execute("""
        SELECT 1 FROM sendgrid_events
        WHERE lower(trim(email))=? AND lower(event_type) IN ({})
        LIMIT 1
    """.format(",".join("?" for _ in SUPPRESSION_TYPES)),
        (email.strip().lower(), *SUPPRESSION_TYPES)).fetchone() is not None


def _available_candidates(cur, tag_id, pools, lists, count):
    source_sql = []
    args = [tag_id]
    if UNIFIED_POOL in pools:
        source_sql.append("""(p.pool_type<>? OR EXISTS (
            SELECT 1 FROM recipient_pool_list_members lm
            JOIN recipient_lists l ON l.id=lm.list_id
            WHERE lm.pool_id=p.id AND l.tag_id=p.tag_id AND l.status='active'
              AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
        ))""")
        args.append(NAMED_POOL)
    elif pools:
        source_sql.append("p.pool_type IN ({})".format(",".join("?" for _ in pools)))
        args.extend(pools)
    if lists:
        source_sql.append("""
            EXISTS (
                SELECT 1 FROM recipient_pool_list_members lm
                WHERE lm.pool_id=p.id AND lm.list_id IN ({})
            )
        """.format(",".join("?" for _ in lists)))
        args.extend(lists)
    # BEGIN IMMEDIATE makes selection and reservation atomic across all workers.
    sql = """
        WITH eligible AS (
            SELECT p.id, p.email, p.name, p.pool_type,
                   ROW_NUMBER() OVER (PARTITION BY lower(trim(p.email)) ORDER BY p.id) AS rank_in_tag
            FROM recipient_pool p
            WHERE p.tag_id=? AND p.status='available' AND ({sources})
              AND NOT EXISTS (
                  SELECT 1 FROM scheduled_email_tasks s
                  WHERE s.tag_id=p.tag_id AND lower(trim(s.recipient_email))=lower(trim(p.email))
                    AND s.status IN ('pending','sending','sent','needs_review')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM recipient_pool other_pool
                  WHERE other_pool.tag_id=p.tag_id AND other_pool.id<>p.id
                    AND lower(trim(other_pool.email))=lower(trim(p.email))
                    AND other_pool.status IN ('reserved','sent','failed')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM send_log l
                  LEFT JOIN mail_tasks m ON m.id=l.task_id
                  LEFT JOIN send_channels sent_channel ON sent_channel.id=l.channel_id
                  WHERE (COALESCE(m.tag_id,sent_channel.tag_id)=p.tag_id
                         OR (m.tag_id IS NULL AND sent_channel.tag_id IS NULL))
                    AND lower(trim(l.recipient_email))=lower(trim(p.email))
                    AND l.status='sent'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM sendgrid_events e
                  WHERE lower(trim(e.email))=lower(trim(p.email))
                    AND lower(e.event_type) IN ({suppression})
              )
        )
        SELECT id, email, name, pool_type FROM eligible
        WHERE rank_in_tag=1 ORDER BY id LIMIT ?
    """.format(sources=" OR ".join(source_sql),
               suppression=",".join("?" for _ in SUPPRESSION_TYPES))
    return [dict(row) for row in cur.execute(sql, (*args, *SUPPRESSION_TYPES, count)).fetchall()]


def _local_iso(epoch):
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def start_task(task_id, now_epoch=None):
    epoch = int(time.time() if now_epoch is None else now_epoch)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        task_row = cur.execute("SELECT * FROM mail_tasks WHERE id=? AND task_kind='warmup'", (task_id,)).fetchone()
        if not task_row:
            raise ValueError("预热任务不存在。")
        task = dict(task_row)
        if task["status"] == "running" and task["warmup_anchor_epoch"] is not None:
            conn.commit()
            return {"created": 0, "anchor_epoch": task["warmup_anchor_epoch"]}
        if task["status"] != "draft" or task["warmup_anchor_epoch"] is not None:
            raise ValueError("任务已开始；恢复已暂停任务请使用继续。")
        channel, templates = _resources(cur, task["tag_id"], task["channel_id"], task["template_group_id"])
        plan_days = [dict(row) for row in cur.execute(
            "SELECT * FROM warmup_day_plans WHERE task_id=? ORDER BY day_index", (task_id,)
        )]
        counts = [int(day["quota"]) for day in plan_days]
        _validate_counts(counts, task["warmup_interval_mode"], task["warmup_interval_seconds"] or "")
        source_rows = cur.execute(
            "SELECT source_type, source_id FROM warmup_task_sources WHERE task_id=?", (task_id,)
        ).fetchall()
        pools = [row["source_id"] for row in source_rows if row["source_type"] == "pool"]
        lists = [int(row["source_id"]) for row in source_rows if row["source_type"] == "list"]
        _sources(cur, task["tag_id"], pools, lists)
        total = sum(counts)
        selected = _available_candidates(cur, task["tag_id"], pools, lists, total)
        if len(selected) < total:
            raise ValueError("可用且去重、排除退订后的收件人不足：需要 {}，实际 {}。".format(total, len(selected)))
        due_items = []
        offset = 0
        for day in plan_days:
            day_index, quota = day["day_index"], day["quota"]
            window_start = epoch + DAY * day_index
            window_end = window_start + DAY
            for position in range(quota):
                step = ((position * DAY) // quota if task["warmup_interval_mode"] == "auto"
                        else position * task["warmup_interval_seconds"])
                due_items.append((selected[offset], day_index, window_start + step, window_end))
                offset += 1
        # Channel daily_limit is the existing local calendar-day safety cap.
        per_calendar_day = Counter(_local_iso(due)[:10] for _, _, due, _ in due_items)
        daily_limit = int(channel["daily_limit"] or 0)
        if daily_limit <= 0:
            raise ValueError("通道每日限额必须大于零。")
        for date, requested in per_calendar_day.items():
            planned = cur.execute("""
                SELECT COUNT(*) AS c FROM scheduled_email_tasks
                WHERE channel_id=? AND substr(scheduled_at,1,10)=?
                  AND status IN ('pending','sending','sent','needs_review')
            """, (task["channel_id"], date)).fetchone()["c"]
            stats = cur.execute(
                "SELECT sent_count+reserved_count AS c FROM channel_daily_stats WHERE channel_id=? AND date=?",
                (task["channel_id"], date),
            ).fetchone()
            occupied = max(planned, int(stats["c"] or 0) if stats else 0)
            if occupied + requested > daily_limit:
                raise ValueError("通道 {} 的 {} 计划超出自然日限额（已有 {}，新增 {}，上限 {}）。".format(
                    channel["name"], date, occupied, requested, daily_limit))
        stamp = now_iso()
        cur.executemany("""
            UPDATE warmup_day_plans
            SET window_start_epoch=?, window_end_epoch=?
            WHERE task_id=? AND day_index=?
        """, [(epoch + DAY * day["day_index"], epoch + DAY * (day["day_index"] + 1),
               task_id, day["day_index"]) for day in plan_days])
        rows = []
        for recipient, day_index, due, end in due_items:
            cur.execute("""
                UPDATE recipient_pool
                SET status='reserved', reserved_task_id=?, reserved_at=?, updated_at=?
                WHERE id=? AND tag_id=? AND lower(trim(email))=? AND status='available'
            """, (task_id, stamp, stamp, recipient["id"], task["tag_id"],
                  recipient["email"].strip().lower()))
            if cur.rowcount != 1:
                raise ValueError("收件人库存发生变化，请重新启动任务。")
            token = code8()
            variables = {"from_mail": channel["from_email"],
                         "to_email": recipient["email"], "code8": token}
            template = random.choice(templates)
            subject_template = template.get("subject_template") or task["subject_template"]
            if not subject_template:
                raise ValueError("HTML 模板未绑定主题，且任务没有默认主题。")
            from_name = template.get("from_name")
            if from_name is None:
                from_name = channel["from_name"] or ""
            rows.append((task_id, task["tag_id"], task["channel_id"], recipient["email"],
                         recipient["name"] or "", channel["from_email"], from_name,
                         subject_template, render_vars(subject_template, variables),
                         template["file_path"], token, _local_iso(due), "pending", 0, stamp,
                         recipient["id"], recipient["pool_type"], day_index, due, end))
        cur.executemany("""
            INSERT INTO scheduled_email_tasks
                (task_id, tag_id, channel_id, recipient_email, recipient_name, from_email, from_name,
                 subject_template, subject_rendered, html_file, code8, scheduled_at, status, attempts,
                 created_at, recipient_pool_id, recipient_pool_type, warmup_day_index,
                 warmup_due_epoch, warmup_end_epoch)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        cur.execute("""
            UPDATE mail_tasks SET warmup_anchor_epoch=?, warmup_next_eligible_epoch=?,
                status='running', updated_at=? WHERE id=? AND status='draft'
        """, (epoch, epoch, stamp, task_id))
        conn.commit()
        return {"created": total, "anchor_epoch": epoch}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pause_task(task_id):
    return _set_status(task_id, "running", "paused")


def resume_task(task_id):
    return _set_status(task_id, "paused", "running")


def _set_status(task_id, before, after):
    conn = get_conn()
    try:
        cur = conn.execute("""
            UPDATE mail_tasks SET status=?, updated_at=?
            WHERE id=? AND task_kind='warmup' AND status=?
        """, (after, now_iso(), task_id, before))
        conn.commit()
        if cur.rowcount != 1:
            raise ValueError("当前任务状态不允许此操作。")
        return True
    finally:
        conn.close()


def delete_task(task_id):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT id FROM mail_tasks WHERE id=? AND task_kind='warmup'", (task_id,)).fetchone()
        if not task:
            raise ValueError("预热任务不存在。")
        active = conn.execute("""
            SELECT COUNT(*) AS c FROM scheduled_email_tasks
            WHERE task_id=? AND status IN ('sending','sent','needs_review')
        """, (task_id,)).fetchone()["c"]
        if active:
            raise ValueError("任务有已接受或结果不明的邮件，请保留审计记录。可以先暂停任务。")
        conn.execute("""
            UPDATE recipient_pool SET status='available', reserved_task_id=NULL,
                reserved_schedule_id=NULL, reserved_at=NULL, updated_at=?
            WHERE reserved_task_id=? AND status='reserved'
        """, (now_iso(), task_id))
        conn.execute("DELETE FROM scheduled_email_tasks WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM warmup_day_plans WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM warmup_task_sources WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM mail_tasks WHERE id=?", (task_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_recipient(cur, schedule):
    if schedule["recipient_pool_id"]:
        cur.execute("""
            UPDATE recipient_pool SET status='available', reserved_task_id=NULL,
                reserved_schedule_id=NULL, reserved_at=NULL, updated_at=?
            WHERE id=? AND reserved_task_id=? AND status='reserved'
        """, (now_iso(), schedule["recipient_pool_id"], schedule["task_id"]))


def _expire_and_recover(cur, epoch):
    expired = cur.execute("""
        SELECT s.* FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
        WHERE m.task_kind='warmup' AND s.status='pending' AND s.warmup_end_epoch<=?
    """, (epoch,)).fetchall()
    for schedule in expired:
        cur.execute("UPDATE scheduled_email_tasks SET status='missed',last_error='24-hour window expired' WHERE id=? AND status='pending'",
                    (schedule["id"],))
        _release_recipient(cur, schedule)
    for task_id in {schedule["task_id"] for schedule in expired}:
        _complete_if_finished(cur, task_id)
    timeout = max(get_settings().worker_claim_timeout_seconds,
                  get_settings().request_timeout_seconds * 2)
    # Ambiguous network outcome: keep channel capacity and recipient reserved.
    stale = cur.execute("""
        SELECT s.id FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
        WHERE m.task_kind='warmup' AND s.status='sending'
          AND (s.claimed_epoch IS NULL OR s.claimed_epoch<=?)
    """, (epoch - timeout,)).fetchall()
    for row in stale:
        cur.execute("""
            UPDATE scheduled_email_tasks SET status='needs_review',worker_id=NULL,
                last_error='Worker stopped during send; check SendGrid before resolving'
            WHERE id=? AND status='sending'
        """, (row["id"],))
    return len(expired) + len(stale)


def _recipient_still_allowed(cur, schedule):
    email = schedule["recipient_email"].strip().lower()
    if _suppressed(cur, email):
        return False
    pool = cur.execute("""
        SELECT p.tag_id,p.email,p.status,p.reserved_task_id,p.pool_type
        FROM recipient_pool p WHERE p.id=?
    """, (schedule["recipient_pool_id"],)).fetchone()
    if (not pool or pool["tag_id"] != schedule["tag_id"]
            or pool["email"].strip().lower() != email
            or pool["status"] != "reserved" or pool["reserved_task_id"] != schedule["task_id"]):
        return False
    if cur.execute("""
        SELECT 1 FROM recipient_pool other_pool
        WHERE other_pool.tag_id=? AND other_pool.id<>?
          AND lower(trim(other_pool.email))=?
          AND other_pool.status IN ('reserved','sent','failed')
        LIMIT 1
    """, (schedule["tag_id"], schedule["recipient_pool_id"], email)).fetchone():
        return False
    pool_source = cur.execute("""
        SELECT source_id FROM warmup_task_sources
        WHERE task_id=? AND source_type='pool' AND source_id IN (?, ?)
        LIMIT 1
    """, (schedule["task_id"], UNIFIED_POOL, pool["pool_type"])).fetchone()
    if pool_source:
        # A legacy named-list row included via the unified pool still requires
        # at least one active list with a recorded source of consent.
        if pool["pool_type"] != NAMED_POOL:
            return True
        return cur.execute("""
            SELECT 1 FROM recipient_pool_list_members lm
            JOIN recipient_lists l ON l.id=lm.list_id
            WHERE lm.pool_id=? AND l.status='active'
              AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
            LIMIT 1
        """, (schedule["recipient_pool_id"],)).fetchone() is not None
    return cur.execute("""
        SELECT 1 FROM recipient_pool_list_members lm
        JOIN recipient_lists l ON l.id=lm.list_id
        JOIN warmup_task_sources src ON src.task_id=? AND src.source_type='list'
            AND src.source_id=CAST(l.id AS TEXT)
        WHERE lm.pool_id=? AND l.tag_id=? AND l.status='active'
          AND l.consent_source IS NOT NULL AND l.consented_at IS NOT NULL
        LIMIT 1
    """, (schedule["task_id"], schedule["recipient_pool_id"], schedule["tag_id"])).fetchone() is not None


def _claim_one(epoch, skip_ids):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        _expire_and_recover(cur, epoch)
        exclusions = (" AND s.id NOT IN ({})".format(",".join("?" for _ in skip_ids)) if skip_ids else "")
        schedule = cur.execute("""
            SELECT s.*,m.warmup_interval_mode,m.warmup_interval_seconds,
                m.warmup_next_eligible_epoch,c.daily_limit,c.status AS channel_status,
                c.tag_id AS channel_tag_id,t.status AS tag_status,
                g.status AS template_status,g.tag_id AS template_tag_id,
                m.tag_id AS task_tag_id,m.channel_id AS task_channel_id
            FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            JOIN send_channels c ON c.id=s.channel_id
            JOIN tags t ON t.id=s.tag_id
            JOIN template_groups g ON g.id=m.template_group_id
            WHERE m.task_kind='warmup' AND m.status='running'
              AND s.status='pending' AND s.warmup_due_epoch<=?
              AND s.warmup_end_epoch>?
              AND (COALESCE(m.warmup_next_eligible_epoch,0)<=?
                   OR s.warmup_day_index > COALESCE((
                       SELECT MAX(previous.warmup_day_index)
                       FROM scheduled_email_tasks previous
                       WHERE previous.task_id=m.id AND previous.attempts>0
                   ),-1))
              AND NOT EXISTS (
                  SELECT 1 FROM scheduled_email_tasks in_flight
                  WHERE in_flight.task_id=m.id AND in_flight.status IN ('sending','needs_review')
              )
              {exclusions}
            ORDER BY s.warmup_due_epoch,s.id LIMIT 1
        """.format(exclusions=exclusions), (epoch, epoch, epoch, *skip_ids)).fetchone()
        if not schedule:
            conn.commit()
            return None, None
        schedule = dict(schedule)
        if (not _recipient_still_allowed(cur, schedule) or schedule["channel_status"] != "active"
                or schedule["tag_status"] != "active" or schedule["template_status"] != "active"
                or schedule["task_tag_id"] != schedule["tag_id"]
                or schedule["task_channel_id"] != schedule["channel_id"]
                or schedule["channel_tag_id"] != schedule["tag_id"]
                or schedule["template_tag_id"] != schedule["tag_id"]):
            cur.execute("UPDATE scheduled_email_tasks SET status='missed',last_error='Source disabled or address suppressed' WHERE id=?",
                        (schedule["id"],))
            _release_recipient(cur, schedule)
            _complete_if_finished(cur, schedule["task_id"])
            conn.commit()
            return "skipped", schedule["id"]
        channel_date = _local_iso(epoch)[:10]
        cur.execute("""
            INSERT OR IGNORE INTO channel_daily_stats
                (channel_id,date,sent_count,failed_count,reserved_count)
            VALUES (?,?,0,0,0)
        """, (schedule["channel_id"], channel_date))
        cur.execute("""
            UPDATE channel_daily_stats SET reserved_count=reserved_count+1
            WHERE channel_id=? AND date=?
              AND sent_count+reserved_count<?
        """, (schedule["channel_id"], channel_date, int(schedule["daily_limit"] or 0)))
        if cur.rowcount != 1:
            # Keep expired-message cleanup and stale-claim review committed.
            # No channel slot was reserved by this failed conditional update.
            conn.commit()
            return "blocked", schedule["id"]
        worker_id = uuid.uuid4().hex
        cur.execute("""
            UPDATE scheduled_email_tasks
            SET status='sending',claimed_at=?,claimed_epoch=?,worker_id=?,
                channel_slot_date=?,attempts=attempts+1
            WHERE id=? AND status='pending' AND warmup_end_epoch>?
        """, (_local_iso(epoch), epoch, worker_id, channel_date, schedule["id"], epoch))
        if cur.rowcount != 1:
            conn.rollback()
            return None, None
        day_quota = cur.execute("""
            SELECT quota FROM warmup_day_plans WHERE task_id=? AND day_index=?
        """, (schedule["task_id"], schedule["warmup_day_index"])).fetchone()
        attempted = cur.execute("""
            SELECT COUNT(*) AS c FROM scheduled_email_tasks
            WHERE task_id=? AND warmup_day_index=? AND attempts>0
        """, (schedule["task_id"], schedule["warmup_day_index"])).fetchone()["c"]
        if not day_quota or attempted > day_quota["quota"]:
            conn.rollback()
            return "blocked", schedule["id"]
        interval = (DAY // day_quota["quota"] if schedule["warmup_interval_mode"] == "auto"
                    else int(schedule["warmup_interval_seconds"]))
        cur.execute("""
            UPDATE mail_tasks SET warmup_next_eligible_epoch=?
            WHERE id=? AND status='running'
        """, (min(schedule["warmup_end_epoch"], epoch + max(1, interval)),
              schedule["task_id"]))
        conn.commit()
        return schedule, worker_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_send(schedule, worker_id, ok, raw, error, finished_epoch=None):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        row = cur.execute("""
            SELECT * FROM scheduled_email_tasks WHERE id=? AND status='sending' AND worker_id=?
        """, (schedule["id"], worker_id)).fetchone()
        if not row:
            conn.commit()
            return
        # The HTTP call may have reached SendGrid without a response.
        uncertain = not ok and raw is None
        result = "sent" if ok else ("needs_review" if uncertain else "failed")
        cur.execute("""
            UPDATE scheduled_email_tasks
            SET status=?,sent_at=?,sender_response=?,last_error=?,worker_id=NULL
            WHERE id=? AND status='sending' AND worker_id=?
        """, (result, now_iso() if ok else None, str(raw or "")[:10000], error,
              schedule["id"], worker_id))
        if not uncertain:
            cur.execute("""
                UPDATE channel_daily_stats
                SET reserved_count=MAX(0,reserved_count-1),
                    sent_count=sent_count+?,failed_count=failed_count+?
                WHERE channel_id=? AND date=?
            """, (1 if ok else 0, 0 if ok else 1,
                  schedule["channel_id"], row["channel_slot_date"]))
            if ok:
                cur.execute("""
                    UPDATE recipient_pool SET status='sent',sent_at=?,updated_at=?
                    WHERE id=? AND reserved_task_id=?
                """, (now_iso(), now_iso(), schedule["recipient_pool_id"], schedule["task_id"]))
            else:
                _release_recipient(cur, row)
        day = cur.execute("""
            SELECT quota FROM warmup_day_plans WHERE task_id=? AND day_index=?
        """, (schedule["task_id"], schedule["warmup_day_index"])).fetchone()
        interval = (DAY // day["quota"] if schedule["warmup_interval_mode"] == "auto"
                    else int(schedule["warmup_interval_seconds"]))
        completed_at = int(time.time() if finished_epoch is None else finished_epoch)
        cur.execute("""
            UPDATE mail_tasks
            SET warmup_next_eligible_epoch=MAX(COALESCE(warmup_next_eligible_epoch,0),?)
            WHERE id=?
        """, (min(schedule["warmup_end_epoch"], completed_at + max(1, interval)),
              schedule["task_id"]))
        _complete_if_finished(cur, schedule["task_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _complete_if_finished(cur, task_id):
    unfinished = cur.execute("""
        SELECT 1 FROM scheduled_email_tasks
        WHERE task_id=? AND status IN ('pending','sending','needs_review') LIMIT 1
    """, (task_id,)).fetchone()
    if not unfinished:
        cur.execute("""
            UPDATE mail_tasks SET status='completed',updated_at=?
            WHERE id=? AND task_kind='warmup' AND status IN ('running','paused')
        """, (now_iso(), task_id))


def _local_send_error(schedule):
    """Detect errors before calling the provider, with an unambiguous outcome."""
    try:
        channel = q_one("SELECT * FROM send_channels WHERE id=?", (schedule["channel_id"],))
        if (not channel or channel["status"] != "active"
                or channel["tag_id"] != schedule["tag_id"]):
            return "Channel is missing, inactive, or belongs to another tag"
        tag = q_one("SELECT service_type,status FROM tags WHERE id=?", (schedule["tag_id"],))
        if not tag or tag["status"] != "active":
            return "Tag is missing or inactive"
        html = Path(schedule["html_file"]).read_text(encoding="utf-8", errors="ignore")
        if not unprotect(channel["api_key_protected"]):
            return "Channel API key is empty"
        proxies = services._build_proxies(channel)
        if channel.get("proxy_id") and not proxies:
            return "Configured proxy is unavailable"
        if get_settings().require_unsubscribe_for_marketing and tag["service_type"] == "marketing":
            variables = {
                "from_mail": schedule["from_email"],
                "to_email": schedule["recipient_email"],
                "code8": schedule["code8"],
            }
            rendered = render_vars(html, variables).lower()
            if "unsubscribe" not in rendered and "退订" not in rendered:
                return "Marketing HTML does not contain unsubscribe keyword/link"
    except Exception as exc:
        return "Local send preparation failed: {}".format(str(exc)[:400])
    return None


def process_due_tasks(limit, now_epoch=None):
    """Claim at most one message per task interval; never catch up in a burst."""
    processed, skipped = 0, set()
    max_count = max(1, int(limit))
    for _ in range(max_count):
        epoch = int(time.time() if now_epoch is None else now_epoch)
        item, worker_id = _claim_one(epoch, skipped)
        if item is None:
            break
        if item in ("blocked", "skipped"):
            skipped.add(worker_id)
            continue
        local_error = _local_send_error(item)
        if local_error:
            ok, raw, error = False, "NOT_SENT", local_error
            channel = q_one("SELECT * FROM send_channels WHERE id=?", (item["channel_id"],))
            services._log_send_attempt(
                item, channel, item.get("subject_rendered") or "", None, None,
                "failed", error, None, None,
            )
        else:
            try:
                ok, _message_id, error, raw = services._send_via_sendgrid(item)
            except Exception as exc:
                ok, raw, error = False, None, str(exc)[:500]
            if not ok and raw is None and error in (
                    "Channel not found", "Channel is not active",
                    "Marketing HTML does not contain unsubscribe keyword/link"):
                raw = "NOT_SENT"
        if not ok and raw is not None and raw != "NOT_SENT":
            log_row = q_one("""
                SELECT http_status FROM send_log
                WHERE scheduled_task_id=? ORDER BY id DESC LIMIT 1
            """, (item["id"],))
            status = log_row["http_status"] if log_row else None
            if status in (408, 429) or (status is not None and status >= 500):
                # An upstream error may occur after the provider accepted the mail.
                raw = None
        _finish_send(item, worker_id, ok, raw, error, now_epoch)
        processed += 1
    return processed


def resolve_review(schedule_id, resolution):
    if resolution not in ("accepted", "failed"):
        raise ValueError("核销结果必须为已接受或明确失败。")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        row = cur.execute("""
            SELECT s.* FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            WHERE s.id=? AND m.task_kind='warmup' AND s.status='needs_review'
        """, (schedule_id,)).fetchone()
        if not row:
            raise ValueError("邮件不处于待人工核销状态。")
        accepted = resolution == "accepted"
        cur.execute("""
            UPDATE scheduled_email_tasks SET status=?,sent_at=?,last_error=?,
                channel_slot_date=NULL,claimed_at=NULL,claimed_epoch=NULL
            WHERE id=? AND status='needs_review'
        """, ("sent" if accepted else "failed", now_iso() if accepted else None,
              "Manually resolved: " + resolution, schedule_id))
        if row["channel_slot_date"]:
            cur.execute("""
                UPDATE channel_daily_stats
                SET reserved_count=MAX(0,reserved_count-1),
                    sent_count=sent_count+?,failed_count=failed_count+?
                WHERE channel_id=? AND date=?
            """, (1 if accepted else 0, 0 if accepted else 1,
                  row["channel_id"], row["channel_slot_date"]))
        if accepted:
            cur.execute("""
                UPDATE recipient_pool SET status='sent',sent_at=?,updated_at=?
                WHERE id=? AND reserved_task_id=?
            """, (now_iso(), now_iso(), row["recipient_pool_id"], row["task_id"]))
        else:
            _release_recipient(cur, row)
        _complete_if_finished(cur, row["task_id"])
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dashboard_data():
    tasks = q_all("""
        SELECT m.id,m.name,m.tag_id,m.channel_id,m.status,m.warmup_anchor_epoch,
            m.warmup_days,m.warmup_interval_mode,t.name AS tag_name,c.name AS channel_name
        FROM mail_tasks m JOIN tags t ON t.id=m.tag_id
        JOIN send_channels c ON c.id=m.channel_id
        WHERE m.task_kind='warmup' ORDER BY m.id DESC LIMIT 100
    """)
    for task in tasks:
        task["days"] = q_all("""
            SELECT d.day_index,d.quota,d.window_start_epoch,d.window_end_epoch,
                SUM(CASE WHEN s.status='sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN s.status='missed' THEN 1 ELSE 0 END) AS missed,
                SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending
            FROM warmup_day_plans d LEFT JOIN scheduled_email_tasks s
                ON s.task_id=d.task_id AND s.warmup_day_index=d.day_index
            WHERE d.task_id=? GROUP BY d.task_id,d.day_index ORDER BY d.day_index
        """, (task["id"],))
        task["total_planned"] = sum(day["quota"] for day in task["days"])
        task["total_sent"] = sum(day["sent"] for day in task["days"])
        task["total_failed"] = sum(day["failed"] for day in task["days"])
        task["total_missed"] = sum(day["missed"] for day in task["days"])
        next_due = q_one("""
            SELECT MIN(warmup_due_epoch) AS due FROM scheduled_email_tasks
            WHERE task_id=? AND status='pending'
        """, (task["id"],))
        task["next_due_epoch"] = next_due["due"] if next_due else None
    return {
        "warmup_tasks": tasks,
        "warmup_lists": q_all("""
            SELECT l.id,l.tag_id,l.name,l.consent_source,l.consented_at,
              COUNT(DISTINCT CASE WHEN p.status='available'
                AND NOT EXISTS (
                    SELECT 1 FROM recipient_pool other_pool
                    WHERE other_pool.tag_id=p.tag_id AND other_pool.id<>p.id
                      AND lower(trim(other_pool.email))=lower(trim(p.email))
                      AND other_pool.status IN ('reserved','sent','failed')
                )
                AND NOT EXISTS (
                    SELECT 1 FROM scheduled_email_tasks s
                    WHERE s.tag_id=p.tag_id
                      AND lower(trim(s.recipient_email))=lower(trim(p.email))
                      AND s.status IN ('pending','sending','sent','needs_review')
                )
                AND NOT EXISTS (
                    SELECT 1 FROM send_log log
                    LEFT JOIN mail_tasks task ON task.id=log.task_id
                    LEFT JOIN send_channels channel ON channel.id=log.channel_id
                    WHERE (COALESCE(task.tag_id,channel.tag_id)=p.tag_id
                           OR (task.tag_id IS NULL AND channel.tag_id IS NULL))
                      AND lower(trim(log.recipient_email))=lower(trim(p.email))
                      AND log.status='sent'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM sendgrid_events e
                    WHERE lower(trim(e.email))=lower(trim(p.email))
                      AND lower(e.event_type) IN ('unsubscribe','group_unsubscribe',
                                                  'spamreport','spam report','bounce')
                )
                THEN lower(trim(p.email)) END) AS available_count
            FROM recipient_lists l
            LEFT JOIN recipient_pool_list_members lm ON lm.list_id=l.id
            LEFT JOIN recipient_pool p ON p.id=lm.pool_id
            WHERE l.list_group=? AND l.status='active'
            GROUP BY l.id ORDER BY l.id DESC LIMIT 500
        """, (NAMED_POOL,)),
        "warmup_reviews": q_all("""
            SELECT s.id,s.task_id,s.recipient_email,s.claimed_at
            FROM scheduled_email_tasks s JOIN mail_tasks m ON m.id=s.task_id
            WHERE m.task_kind='warmup' AND s.status='needs_review'
            ORDER BY s.id DESC LIMIT 100
        """),
    }
