import base64
import hashlib
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_TEST_DIR = Path(tempfile.mkdtemp(prefix="sendgrid-web-tests-"))
os.environ["DATABASE_PATH"] = str(_TEST_DIR / "test.db")
os.environ["TEMPLATE_STORAGE_DIR"] = str(_TEST_DIR / "templates")
os.environ["UPLOAD_DIR"] = str(_TEST_DIR / "uploads")
os.environ["SECRET_KEY"] = "session-" + "a" * 48
os.environ["DATA_ENCRYPTION_KEY"] = "data-" + "b" * 48
os.environ["SERVICE_TOKEN"] = "token-" + "c" * 48
os.environ["ENVIRONMENT"] = "development"
os.environ["STORE_SEND_REQUEST_BODY"] = "false"
os.environ["WORKER_CLAIM_TIMEOUT_SECONDS"] = "60"
os.environ["REQUEST_TIMEOUT_SECONDS"] = "5"

from app import services, warmup  # noqa: E402
from app.crypto import protect, unprotect  # noqa: E402
from app.db import execute, get_conn, init_db, q_all, q_one  # noqa: E402


class ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.template_file = b"<html>Hello {{to_email}}</html>"

    def create_resources(self, suffix, daily_limit=2):
        tag_id = services.create_tag("tag-" + suffix, "transactional", "")
        channel_id = services.create_channel(
            tag_id,
            "channel-" + suffix,
            "SG.fake-" + suffix,
            "sender-{}@example.com".format(suffix),
            "Sender",
            None,
            daily_limit,
        )
        group_id = services.create_template_group(tag_id, "group-" + suffix)
        services.save_template_file(group_id, "template.html", self.template_file)
        return tag_id, channel_id, group_id

    def add_pool(self, tag_id, pool_type, prefix, count):
        stamp = services.now_iso()
        rows = [
            (
                tag_id,
                "{}-{}@example.com".format(prefix, index),
                "User {}".format(index),
                pool_type,
                "available",
                "test",
                stamp,
                stamp,
            )
            for index in range(count)
        ]
        conn = get_conn()
        try:
            conn.executemany(
                """
                INSERT INTO recipient_pool (
                    tag_id, email, name, pool_type, status, source_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def seed_plan_pool(self, tag_id, prefix, daily_limit=2):
        self.add_pool(tag_id, services.POOL_UNIFIED, prefix, daily_limit * 30)

    def test_concurrent_generation_is_atomic_and_capacity_aware(self):
        tag_id = services.create_tag("concurrent", "transactional", "")
        group_id = services.create_template_group(tag_id, "concurrent-group")
        services.save_template_file(group_id, "same.html", self.template_file)
        channel_1 = services.create_channel(
            tag_id, "concurrent-1", "SG.one", "one@example.com", "One", None, 2
        )
        channel_2 = services.create_channel(
            tag_id, "concurrent-2", "SG.two", "two@example.com", "Two", None, 2
        )
        # Two plans need 120 unique addresses in total.
        self.add_pool(tag_id, services.POOL_UNIFIED, "concurrent", 120)
        task_1 = services.create_mail_task(tag_id, channel_1, "task-1", "Hello", group_id)
        task_2 = services.create_mail_task(tag_id, channel_2, "task-2", "Hello", group_id)

        results = {}
        errors = []

        def generate(name, task_id):
            try:
                results[name] = services.generate_plan(task_id)
            except Exception as exc:  # pragma: no cover - captured for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=generate, args=("one", task_1)),
            threading.Thread(target=generate, args=("two", task_2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(results["one"]["created"], 60)
        self.assertEqual(results["two"]["created"], 60)

        pool_1 = {
            row["recipient_pool_id"]
            for row in q_all(
                "SELECT recipient_pool_id FROM scheduled_email_tasks WHERE task_id=?",
                (task_1,),
            )
        }
        pool_2 = {
            row["recipient_pool_id"]
            for row in q_all(
                "SELECT recipient_pool_id FROM scheduled_email_tasks WHERE task_id=?",
                (task_2,),
            )
        }
        self.assertTrue(pool_1.isdisjoint(pool_2))

        for task_id in (task_1, task_2):
            daily = q_all(
                """
                SELECT substr(scheduled_at, 1, 10) AS day_key, COUNT(*) AS c
                FROM scheduled_email_tasks WHERE task_id=? GROUP BY day_key
                """,
                (task_id,),
            )
            self.assertLessEqual(max(int(row["c"]) for row in daily), 2)

    def test_failed_regeneration_keeps_original_plan(self):
        tag_id, channel_id, group_id = self.create_resources("rollback", daily_limit=2)
        self.seed_plan_pool(tag_id, "rollback", daily_limit=2)
        task_id = services.create_mail_task(tag_id, channel_id, "rollback-task", "Hello", group_id)
        services.generate_plan(task_id)
        before_ids = [
            row["id"]
            for row in q_all(
                "SELECT id FROM scheduled_email_tasks WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]
        victim = q_one(
            "SELECT recipient_pool_id FROM scheduled_email_tasks WHERE task_id=? LIMIT 1",
            (task_id,),
        )["recipient_pool_id"]
        execute("DELETE FROM recipient_pool WHERE id=?", (victim,))

        with self.assertRaises(ValueError):
            services.generate_plan(task_id, force=True)

        after_ids = [
            row["id"]
            for row in q_all(
                "SELECT id FROM scheduled_email_tasks WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]
        self.assertEqual(after_ids, before_ids)

    def test_stale_claim_requires_review_and_keeps_daily_slot(self):
        tag_id, channel_id, group_id = self.create_resources("stale", daily_limit=1)
        self.seed_plan_pool(tag_id, "stale", daily_limit=1)
        task_id = services.create_mail_task(tag_id, channel_id, "stale-task", "Hello", group_id)
        services.generate_plan(task_id)
        scheduled_id = q_one(
            "SELECT id FROM scheduled_email_tasks WHERE task_id=? ORDER BY id LIMIT 1",
            (task_id,),
        )["id"]
        worker_id = "dead-worker"
        old_claim_time = datetime.now() - timedelta(minutes=10)
        old_claim = old_claim_time.isoformat(timespec="seconds")
        execute(
            """
            UPDATE scheduled_email_tasks
            SET status='sending', claimed_at=?, claimed_epoch=?, worker_id=?
            WHERE id=?
            """,
            (old_claim, int(old_claim_time.timestamp()), worker_id, scheduled_id),
        )
        slot_date = services._reserve_channel_slot(channel_id, 1, scheduled_id, worker_id)
        self.assertIsNotNone(slot_date)
        self.assertEqual(
            q_one(
                "SELECT reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
                (channel_id, slot_date),
            )["reserved_count"],
            1,
        )

        self.assertEqual(services._recover_stale_sending_claims(), 1)
        row = q_one("SELECT status, channel_slot_date FROM scheduled_email_tasks WHERE id=?", (scheduled_id,))
        self.assertEqual(row["status"], "needs_review")
        self.assertEqual(row["channel_slot_date"], slot_date)
        self.assertEqual(
            q_one(
                "SELECT reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
                (channel_id, slot_date),
            )["reserved_count"],
            1,
        )

    def test_logged_provider_acceptance_is_not_resent_after_worker_crash(self):
        tag_id, channel_id, group_id = self.create_resources("accepted-crash", daily_limit=1)
        self.seed_plan_pool(tag_id, "accepted-crash", daily_limit=1)
        task_id = services.create_mail_task(tag_id, channel_id, "accepted", "Hello", group_id)
        services.generate_plan(task_id)
        scheduled = q_one(
            "SELECT * FROM scheduled_email_tasks WHERE task_id=? ORDER BY id LIMIT 1",
            (task_id,),
        )
        old_claim = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        execute(
            "UPDATE scheduled_email_tasks SET status='sending',claimed_at=?,worker_id=? WHERE id=?",
            (old_claim, "dead-after-202", scheduled["id"]),
        )
        slot_date = services._reserve_channel_slot(channel_id, 1, scheduled["id"], "dead-after-202")
        execute(
            """INSERT INTO send_log
               (scheduled_task_id,task_id,channel_id,recipient_email,http_status,status,created_at)
               VALUES (?,?,?,?,202,'sent',?)""",
            (scheduled["id"], task_id, channel_id, scheduled["recipient_email"], old_claim),
        )
        self.assertEqual(services._recover_stale_sending_claims(), 1)
        self.assertEqual(q_one(
            "SELECT status FROM scheduled_email_tasks WHERE id=?", (scheduled["id"],)
        )["status"], "sent")
        stats = q_one(
            "SELECT sent_count,reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
            (channel_id, slot_date),
        )
        self.assertEqual((stats["sent_count"], stats["reserved_count"]), (1, 0))
        self.assertEqual(q_one(
            "SELECT status FROM recipient_pool WHERE id=?", (scheduled["recipient_pool_id"],)
        )["status"], "sent")

    def test_crypto_and_webhook_deduplication(self):
        value = "SG.secret-value"
        protected = protect(value)
        self.assertTrue(protected.startswith("v2:"))
        self.assertEqual(unprotect(protected), value)

        # Recreate the legacy XOR format to verify upgrade compatibility.
        plain = value.encode("utf-8")
        legacy_secret = os.environ["SECRET_KEY"].encode("utf-8")
        digest = hashlib.sha256(legacy_secret).digest()
        stream = bytearray()
        while len(stream) < len(plain):
            stream.extend(digest)
            digest = hashlib.sha256(digest + legacy_secret).digest()
        legacy = base64.urlsafe_b64encode(
            bytes(left ^ right for left, right in zip(plain, stream[: len(plain)]))
        ).decode("ascii")
        self.assertEqual(unprotect(legacy), value)

        event = {
            "email": "delivered@example.com",
            "event": "delivered",
            "timestamp": 123,
            "sg_message_id": "message-1",
            "sg_event_id": "event-1",
        }
        self.assertEqual(services.record_sendgrid_events([event, event]), 1)

    def test_delete_rejects_active_send_and_last_admin_is_protected(self):
        tag_id, channel_id, group_id = self.create_resources("guards", daily_limit=1)
        self.seed_plan_pool(tag_id, "guards", daily_limit=1)
        task_id = services.create_mail_task(tag_id, channel_id, "guard-task", "Hello", group_id)
        services.generate_plan(task_id)
        execute(
            """
            UPDATE scheduled_email_tasks SET status='sending', claimed_at=?
            WHERE id=(SELECT MIN(id) FROM scheduled_email_tasks WHERE task_id=?)
            """,
            (services.now_iso(), task_id),
        )
        with self.assertRaises(ValueError):
            services.delete_mail_task(task_id)
        self.assertIsNotNone(q_one("SELECT id FROM mail_tasks WHERE id=?", (task_id,)))

        admin = q_one("SELECT id FROM users WHERE role='admin' AND status='active' LIMIT 1")
        with self.assertRaises(ValueError):
            services.update_user(admin["id"], "Admin", "member", "active")

    def test_channel_referenced_by_task_cannot_change_tag(self):
        tag_id, channel_id, group_id = self.create_resources("channel-tag-guard")
        other_tag = services.create_tag("channel-other-tag", "transactional", "")
        task_id = services.create_mail_task(
            tag_id, channel_id, "referencing-draft", "Hello", group_id
        )
        before = q_one("SELECT * FROM send_channels WHERE id=?", (channel_id,))

        with self.assertRaises(ValueError):
            services.update_channel(
                channel_id, other_tag, "moved-channel", "", before["from_email"],
                before["from_name"], before["proxy_id"], before["daily_limit"], "active",
            )
        self.assertEqual(q_one("SELECT * FROM send_channels WHERE id=?", (channel_id,)), before)
        self.assertEqual(q_one("SELECT tag_id FROM mail_tasks WHERE id=?", (task_id,))["tag_id"],
                         tag_id)

    def test_inactive_bound_proxy_blocks_http_post_instead_of_direct_send(self):
        tag_id = services.create_tag("inactive-proxy-tag", "transactional", "")
        proxy_id = services.create_proxy("inactive-proxy", "http://127.0.0.1:3128")
        channel_id = services.create_channel(
            tag_id, "proxied-channel", "SG.fake", "sender@example.com", "Sender",
            proxy_id, 10,
        )
        group_id = services.create_template_group(tag_id, "proxied-template")
        services.save_template_file(group_id, "notice.html", self.template_file)
        imported = warmup.import_named_list(
            tag_id, "proxied-list", "Website opt-in", "2026-09-22T10:00:00+00:00",
            b"proxied@example.com",
        )
        task_id = warmup.create_task(
            tag_id, channel_id, "proxied-task", "Hello", group_id,
            [1], "manual", 60, [], [imported["id"]], True,
        )
        warmup.start_task(task_id, now_epoch=int(datetime.now().timestamp()))
        scheduled = q_one("SELECT * FROM scheduled_email_tasks WHERE task_id=?", (task_id,))
        execute("UPDATE proxies SET status='disabled' WHERE id=?", (proxy_id,))

        with patch.object(services.requests, "post") as post:
            ok, message_id, error, outcome = services._send_via_sendgrid(scheduled)
            post.assert_not_called()
        self.assertFalse(ok)
        self.assertIsNone(message_id)
        self.assertIn("proxy", error.lower())
        self.assertEqual(outcome, "NOT_SENT")

    def test_recipient_pool_edit_delete_search_and_usage_guards(self):
        source_tag = services.create_tag("recipient-source", "transactional", "")
        target_tag = services.create_tag("recipient-target", "transactional", "")
        self.add_pool(source_tag, services.POOL_UNIFIED, "editable", 3)
        editable = q_one(
            "SELECT * FROM recipient_pool WHERE tag_id=? AND pool_type=? ORDER BY id LIMIT 1",
            (source_tag, services.POOL_UNIFIED),
        )

        services.update_recipient_pool_entry(
            editable["id"],
            target_tag,
            services.POOL_UNIFIED,
            "renamed@example.com",
            "Renamed User",
            "manual correction",
        )
        updated = q_one("SELECT * FROM recipient_pool WHERE id=?", (editable["id"],))
        self.assertEqual(updated["tag_id"], target_tag)
        self.assertEqual(updated["pool_type"], services.POOL_UNIFIED)
        self.assertEqual(updated["email"], "renamed@example.com")

        result = services.get_recipient_pool_rows(
            target_tag,
            services.POOL_UNIFIED,
            search="manual correction",
            page=1,
            page_size=10,
        )
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["rows"][0]["identity_editable"])
        self.assertTrue(result["rows"][0]["deletable"])

        services.delete_recipient_pool_entry(editable["id"])
        self.assertIsNone(q_one("SELECT id FROM recipient_pool WHERE id=?", (editable["id"],)))

        tag_id, channel_id, group_id = self.create_resources("recipient-used", daily_limit=1)
        self.seed_plan_pool(tag_id, "recipient-used", daily_limit=1)
        task_id = services.create_mail_task(tag_id, channel_id, "recipient-used-task", "Hello", group_id)
        services.generate_plan(task_id)
        used = q_one(
            """
            SELECT p.* FROM recipient_pool p
            JOIN scheduled_email_tasks s ON s.recipient_pool_id=p.id
            WHERE s.task_id=? ORDER BY p.id LIMIT 1
            """,
            (task_id,),
        )
        services.update_recipient_pool_entry(
            used["id"],
            used["tag_id"],
            used["pool_type"],
            used["email"],
            "Corrected Name",
            "used correction",
        )
        corrected = q_one("SELECT name, source_name FROM recipient_pool WHERE id=?", (used["id"],))
        self.assertEqual(corrected["name"], "Corrected Name")
        self.assertEqual(corrected["source_name"], "used correction")

        with self.assertRaises(ValueError):
            services.update_recipient_pool_entry(
                used["id"],
                used["tag_id"],
                used["pool_type"],
                "changed-after-use@example.com",
                "Corrected Name",
                "used correction",
            )
        with self.assertRaises(ValueError):
            services.delete_recipient_pool_entry(used["id"])

        self.add_pool(tag_id, services.POOL_UNIFIED, "bulk-extra", 2)
        before_used = q_one(
            "SELECT COUNT(*) AS c FROM recipient_pool WHERE tag_id=? AND pool_type=? AND status='reserved'",
            (tag_id, services.POOL_UNIFIED),
        )["c"]
        deleted = services.delete_available_recipient_pool(tag_id, services.POOL_UNIFIED)["deleted"]
        self.assertEqual(deleted, 2)
        after_used = q_one(
            "SELECT COUNT(*) AS c FROM recipient_pool WHERE tag_id=? AND pool_type=? AND status='reserved'",
            (tag_id, services.POOL_UNIFIED),
        )["c"]
        self.assertEqual(after_used, before_used)

    def test_new_pool_import_reuses_address_from_legacy_type(self):
        tag_id = services.create_tag("merged-import", "transactional", "")
        self.add_pool(tag_id, services.POOL_0_3, "historical", 1)
        address = "historical-0@example.com"
        original = q_one(
            "SELECT id FROM recipient_pool WHERE tag_id=? AND email=?",
            (tag_id, address),
        )["id"]
        result = services.import_recipient_pool(
            tag_id, services.POOL_UNIFIED, "new upload",
            (address.upper() + "\nnew@example.com").encode("utf-8"),
        )
        self.assertEqual(result["imported"], 1)
        rows = q_all(
            "SELECT id, email, pool_type FROM recipient_pool WHERE tag_id=? ORDER BY id",
            (tag_id,),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], original)
        self.assertEqual(rows[1]["pool_type"], services.POOL_UNIFIED)

    def test_legacy_draft_rejects_only_unconsented_or_disabled_named_rows(self):
        for case in ("no_consent_record", "disabled_list"):
            with self.subTest(case=case):
                tag_id, channel_id, group_id = self.create_resources(case, daily_limit=1)
                addresses = ["{}-{}@example.com".format(case, n) for n in range(30)]
                if case == "no_consent_record":
                    self.add_pool(tag_id, "warmup_named", case, 30)
                else:
                    result = warmup.import_named_list(
                        tag_id, "historical consenting list", "Website opt-in",
                        "2026-09-22T10:00:00+00:00", "\n".join(addresses).encode("utf-8"),
                    )
                    # This type is how named-list members existed before the
                    # unified pool; their membership must still be checked.
                    execute("UPDATE recipient_pool SET pool_type='warmup_named' WHERE tag_id=?",
                            (tag_id,))
                    warmup.disable_named_list(result["id"])

                task_id = services.create_mail_task(
                    tag_id, channel_id, case, "Hello", group_id
                )
                with self.assertRaises(ValueError):
                    services.generate_plan(task_id)
                self.assertEqual(q_one(
                    "SELECT COUNT(*) AS c FROM scheduled_email_tasks WHERE task_id=?", (task_id,)
                )["c"], 0)

    def test_legacy_pending_named_member_is_skipped_after_list_is_disabled(self):
        tag_id, channel_id, group_id = self.create_resources("disabled-after-plan", daily_limit=1)
        addresses = ["old-list-{}@example.com".format(n) for n in range(30)]
        result = warmup.import_named_list(
            tag_id, "historical list", "Website opt-in",
            "2026-09-22T10:00:00+00:00", "\n".join(addresses).encode("utf-8"),
        )
        execute("UPDATE recipient_pool SET pool_type='warmup_named' WHERE tag_id=?", (tag_id,))
        task_id = services.create_mail_task(
            tag_id, channel_id, "historical plan", "Hello", group_id
        )
        services.generate_plan(task_id)
        services.start_task(task_id)
        planned = q_one(
            "SELECT id,recipient_pool_id FROM scheduled_email_tasks WHERE task_id=? ORDER BY id LIMIT 1",
            (task_id,),
        )
        execute("UPDATE scheduled_email_tasks SET scheduled_at='2999-01-01T00:00:00' WHERE task_id=?",
                (task_id,))
        execute("UPDATE scheduled_email_tasks SET scheduled_at=? WHERE id=?",
                ((datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds"), planned["id"]))
        warmup.disable_named_list(result["id"])

        with patch.object(services.requests, "post") as sender:
            services.process_due_tasks(1)
            sender.assert_not_called()
        self.assertEqual(q_one(
            "SELECT status FROM scheduled_email_tasks WHERE id=?", (planned["id"],)
        )["status"], "missed")
        self.assertEqual(q_one(
            "SELECT status FROM recipient_pool WHERE id=?", (planned["recipient_pool_id"],)
        )["status"], "available")
        self.assertEqual(q_one(
            "SELECT COUNT(*) AS c FROM send_log WHERE scheduled_task_id=?", (planned["id"],)
        )["c"], 0)

    def test_legacy_ambiguous_post_is_reviewed_then_reconciled_once(self):
        tag_id, channel_id, group_id = self.create_resources("ambiguous", daily_limit=1)
        self.seed_plan_pool(tag_id, "ambiguous", daily_limit=1)
        task_id = services.create_mail_task(tag_id, channel_id, "legacy-ambiguous", "Hello", group_id)
        services.generate_plan(task_id)
        services.start_task(task_id)
        scheduled = q_one(
            "SELECT id, recipient_pool_id FROM scheduled_email_tasks WHERE task_id=? ORDER BY id LIMIT 1",
            (task_id,),
        )
        execute("UPDATE scheduled_email_tasks SET scheduled_at='2999-01-01T00:00:00' WHERE task_id=?",
                (task_id,))
        execute("UPDATE scheduled_email_tasks SET scheduled_at=? WHERE id=?",
                ((datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds"), scheduled["id"]))

        with patch.object(
            services.requests, "post",
            side_effect=services.requests.exceptions.ReadTimeout("response lost after POST"),
        ) as sender:
            services.process_due_tasks(1)
            services.process_due_tasks(1)
            self.assertEqual(sender.call_count, 1)

        row = q_one("SELECT status,channel_slot_date FROM scheduled_email_tasks WHERE id=?",
                    (scheduled["id"],))
        self.assertEqual(row["status"], "needs_review")
        self.assertIsNotNone(row["channel_slot_date"])
        self.assertEqual(q_one(
            "SELECT status FROM recipient_pool WHERE id=?", (scheduled["recipient_pool_id"],)
        )["status"], "reserved")
        before = q_one(
            "SELECT sent_count,reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
            (channel_id, row["channel_slot_date"]),
        )
        self.assertEqual((before["sent_count"], before["reserved_count"]), (0, 1))

        services.resolve_legacy_review(scheduled["id"], "accepted")
        after = q_one(
            "SELECT sent_count,reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
            (channel_id, row["channel_slot_date"]),
        )
        self.assertEqual((after["sent_count"], after["reserved_count"]), (1, 0))
        self.assertEqual(q_one(
            "SELECT status FROM recipient_pool WHERE id=?", (scheduled["recipient_pool_id"],)
        )["status"], "sent")
        with self.assertRaises(ValueError):
            services.resolve_legacy_review(scheduled["id"], "accepted")
        self.assertEqual(q_one(
            "SELECT sent_count,reserved_count FROM channel_daily_stats WHERE channel_id=? AND date=?",
            (channel_id, row["channel_slot_date"]),
        ), after)


if __name__ == "__main__":
    unittest.main()
