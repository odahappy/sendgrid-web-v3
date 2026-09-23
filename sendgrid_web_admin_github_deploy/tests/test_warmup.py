"""Behavioral tests for consent-backed, 24-hour warm-up campaigns.

No test makes an HTTP request. The sender is replaced at the existing
services._send_via_sendgrid boundary, and every test gets a fresh SQLite DB.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import services, warmup
from app.db import execute, init_db, q_all, q_one


START_EPOCH = int(datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc).timestamp())
DAY = 24 * 60 * 60


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sendgrid-warmup-tests-")
        base = Path(self.temp.name)
        self.env = {
            "DATABASE_PATH": str(base / "test.db"),
            "TEMPLATE_STORAGE_DIR": str(base / "templates"),
            "UPLOAD_DIR": str(base / "uploads"),
            "SECRET_KEY": "warmup-session-" + "a" * 48,
            "DATA_ENCRYPTION_KEY": "warmup-data-" + "b" * 48,
            "SERVICE_TOKEN": "warmup-token-" + "c" * 48,
            "ENVIRONMENT": "development",
            "STORE_SEND_REQUEST_BODY": "false",
        }
        self.previous_env = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        init_db()
        self.tag = services.create_tag("warmup-test", "transactional", "")
        self.channel = services.create_channel(
            self.tag, "warmup-channel", "SG.test-key", "sender@example.com",
            "Test Sender", None, 500,
        )
        self.group = services.create_template_group(self.tag, "warmup-templates")
        services.save_template_file(
            self.group, "welcome.html", b"<p>Hello {{to_email}}</p>"
        )

    def tearDown(self):
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def add_list(self, *emails, tag_id=None):
        lines = "\n".join(emails).encode("utf-8")
        result = warmup.import_named_list(
            tag_id if tag_id is not None else self.tag,
            "consented-recipients",
            "Website signup form, consent recorded in CRM",
            "2026-09-22T10:00:00+00:00",
            lines,
        )
        if isinstance(result, dict):
            return result.get("list_id") or result.get("id")
        return result

    def add_pool(self, pool_type, *emails):
        for email in emails:
            execute(
                """INSERT INTO recipient_pool
                   (tag_id, email, name, pool_type, status, source_name, created_at, updated_at)
                   VALUES (?, ?, '', ?, 'available', 'consent-backed import', ?, ?)""",
                (self.tag, email.lower(), pool_type, services.now_iso(), services.now_iso()),
            )

    def create_task(self, counts, *, lists=(), pools=(), mode="manual", interval=60,
                    consent=True, tag_id=None, channel_id=None, group_id=None):
        return warmup.create_task(
            tag_id if tag_id is not None else self.tag,
            channel_id if channel_id is not None else self.channel,
            "Warmup test task", "Hello {{to_email}}",
            group_id if group_id is not None else self.group,
            counts, mode, interval, list(pools), list(lists), consent,
        )

    @staticmethod
    def rows(task_id):
        return q_all(
            """SELECT id, recipient_email, recipient_pool_id, html_file, status,
                      warmup_day_index, warmup_due_epoch, warmup_end_epoch
               FROM scheduled_email_tasks WHERE task_id=?
               ORDER BY warmup_due_epoch, id""",
            (task_id,),
        )

    def test_zero_day_single_recipient_and_24_hour_boundaries(self):
        list_id = self.add_list("first@example.com", "second@example.com", "third@example.com")
        task_id = self.create_task([0, 1, 2], lists=[list_id], interval=120)
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        rows = self.rows(task_id)

        self.assertEqual(len(rows), 3)
        self.assertEqual([int(row["warmup_day_index"]) for row in rows], [1, 2, 2])
        self.assertEqual(
            [int(row["warmup_due_epoch"]) for row in rows],
            [START_EPOCH + DAY, START_EPOCH + 2 * DAY, START_EPOCH + 2 * DAY + 120],
        )
        for row in rows:
            day = int(row["warmup_day_index"])
            self.assertGreaterEqual(int(row["warmup_due_epoch"]), START_EPOCH + day * DAY)
            self.assertLess(int(row["warmup_due_epoch"]), START_EPOCH + (day + 1) * DAY)
            self.assertEqual(int(row["warmup_end_epoch"]), START_EPOCH + (day + 1) * DAY)

    def test_auto_spacing_covers_24_hour_window_without_crossing_boundary(self):
        list_id = self.add_list("a@example.com", "b@example.com", "c@example.com")
        task_id = self.create_task([3], lists=[list_id], mode="auto", interval=None)
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        due = [int(row["warmup_due_epoch"]) for row in self.rows(task_id)]
        self.assertEqual(due, [START_EPOCH, START_EPOCH + DAY // 3, START_EPOCH + 2 * DAY // 3])
        self.assertLess(due[-1], START_EPOCH + DAY)

    def test_manual_interval_that_crosses_window_is_rejected(self):
        list_id = self.add_list("a@example.com", "b@example.com", "c@example.com")
        # The third send would land exactly at the next 24-hour window.
        with self.assertRaises(ValueError):
            self.create_task([3], lists=[list_id], interval=DAY // 2)
        with self.assertRaises(ValueError):
            self.create_task([-1], lists=[list_id], interval=10)
        with self.assertRaises(ValueError):
            self.create_task([0, 0], lists=[list_id], interval=10)
        with self.assertRaises(ValueError):
            self.create_task([1], lists=[list_id], interval=0)

    def test_selected_sources_dedupe_across_named_list_and_old_pool(self):
        list_id = self.add_list("SHARED@example.com, Signed Up", "third@example.com")
        self.add_pool(services.POOL_0_3, "first@example.com", "shared@example.com")
        task_id = self.create_task(
            [3], lists=[list_id], pools=[services.POOL_0_3], interval=10
        )
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        rows = self.rows(task_id)
        self.assertEqual(
            {row["recipient_email"].lower() for row in rows},
            {"first@example.com", "shared@example.com", "third@example.com"},
        )
        self.assertTrue(all(row["html_file"] for row in rows))
        self.assertTrue(all(row["status"] == "pending" for row in rows))

    def test_failed_allocation_rolls_back_and_validates_list_ownership(self):
        list_id = self.add_list("a@example.com", "b@example.com")
        task_id = self.create_task([3], lists=[list_id])
        with self.assertRaises(ValueError):
            warmup.start_task(task_id, now_epoch=START_EPOCH)
        self.assertEqual(self.rows(task_id), [])

        # If an unsuccessful allocation left reservations behind, a new
        # two-recipient task cannot use the same consented addresses.
        recovered_id = self.create_task([2], lists=[list_id])
        warmup.start_task(recovered_id, now_epoch=START_EPOCH)
        self.assertEqual(len(self.rows(recovered_id)), 2)

        other_tag = services.create_tag("other-tag", "transactional", "")
        foreign_list = self.add_list("foreign@example.com", tag_id=other_tag)
        with self.assertRaises(ValueError):
            self.create_task([1], lists=[foreign_list])
        with self.assertRaises(ValueError):
            self.create_task([1], lists=[list_id], consent=False)

    def test_manual_spacing_and_sleep_until_next_24_hour_window(self):
        list_id = self.add_list("a@example.com", "b@example.com", "c@example.com")
        task_id = self.create_task([2, 1], lists=[list_id], interval=120)
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        sent = []

        def fake_send(row):
            sent.append(row["id"])
            return True, "fake-message-id", None, "accepted"

        with patch.object(services, "_send_via_sendgrid", side_effect=fake_send):
            warmup.process_due_tasks(10, now_epoch=START_EPOCH)
            self.assertEqual(len(sent), 1)
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + 119)
            self.assertEqual(len(sent), 1)
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + 120)
            self.assertEqual(len(sent), 2)
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + 7200)
            self.assertEqual(len(sent), 2)
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + DAY)
            self.assertEqual(len(sent), 3)

        self.assertEqual(len(set(sent)), 3)
        self.assertEqual([row["status"] for row in self.rows(task_id)], ["sent"] * 3)

    def test_restart_and_late_worker_do_not_burst_or_send_expired_day(self):
        list_id = self.add_list("a@example.com", "b@example.com", "c@example.com", "d@example.com")
        task_id = self.create_task([3, 1], lists=[list_id], interval=60)
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        # A second init simulates opening the database again after restarting.
        init_db()
        sent = []

        def fake_send(row):
            sent.append(row["id"])
            return True, "fake-message-id", None, "accepted"

        with patch.object(services, "_send_via_sendgrid", side_effect=fake_send):
            # The entire first window elapsed while the worker was down.
            for _ in range(5):
                warmup.process_due_tasks(10, now_epoch=START_EPOCH + DAY + 1)
        self.assertEqual(len(sent), 1)
        statuses = self.rows(task_id)
        self.assertEqual([row["status"] for row in statuses if row["warmup_day_index"] == 0],
                         ["missed"] * 3)
        self.assertEqual([row["status"] for row in statuses if row["warmup_day_index"] == 1],
                         ["sent"])

    def test_ambiguous_result_requires_manual_review_and_never_auto_retries(self):
        list_id = self.add_list("one@example.com")
        task_id = self.create_task([1], lists=[list_id])
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        with patch.object(
            services, "_send_via_sendgrid",
            return_value=(False, None, "ReadTimeout after submitting request", None),
        ) as sender:
            warmup.process_due_tasks(10, now_epoch=START_EPOCH)
            self.assertEqual(self.rows(task_id)[0]["status"], "needs_review")
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + 10)
            self.assertEqual(sender.call_count, 1)

        schedule_id = self.rows(task_id)[0]["id"]
        warmup.resolve_review(schedule_id, "accepted")
        self.assertEqual(q_one("SELECT status FROM scheduled_email_tasks WHERE id=?", (schedule_id,))["status"], "sent")
        with patch.object(services, "_send_via_sendgrid") as sender:
            warmup.process_due_tasks(10, now_epoch=START_EPOCH + 100)
            sender.assert_not_called()

    def test_unsubscribe_after_planning_blocks_send_and_releases_address(self):
        list_id = self.add_list("one@example.com")
        task_id = self.create_task([1], lists=[list_id])
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        services.record_sendgrid_events({
            "email": "one@example.com", "event": "unsubscribe",
            "timestamp": START_EPOCH - 1, "sg_event_id": "unsub-1",
        })

        with patch.object(services, "_send_via_sendgrid") as sender:
            warmup.process_due_tasks(10, now_epoch=START_EPOCH)
            sender.assert_not_called()

        self.assertEqual(self.rows(task_id)[0]["status"], "missed")
        self.assertEqual(
            q_one("SELECT status FROM recipient_pool WHERE email=?", ("one@example.com",))["status"],
            "available",
        )

    def test_two_workers_claim_one_warmup_message_only_once(self):
        list_id = self.add_list("one@example.com")
        task_id = self.create_task([1], lists=[list_id])
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        gate = threading.Barrier(3)
        entered = threading.Event()
        release = threading.Event()
        sent, errors = [], []

        def fake_send(row):
            sent.append(row["id"])
            entered.set()
            if not release.wait(5):
                raise AssertionError("Timed out waiting for the other worker")
            return True, "fake-message-id", None, "accepted"

        def run_worker():
            try:
                gate.wait(5)
                warmup.process_due_tasks(1, now_epoch=START_EPOCH)
            except Exception as exc:
                errors.append(exc)

        with patch.object(services, "_send_via_sendgrid", side_effect=fake_send):
            threads = [threading.Thread(target=run_worker, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                gate.wait(5)
                self.assertTrue(entered.wait(5), "No worker reached the mocked sender")
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sent, [self.rows(task_id)[0]["id"]])
        self.assertEqual(self.rows(task_id)[0]["status"], "sent")

    def test_second_worker_cannot_send_same_task_while_first_post_is_in_flight(self):
        list_id = self.add_list("first@example.com", "second@example.com")
        task_id = self.create_task([2], lists=[list_id], interval=1)
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        first_id, second_id = [row["id"] for row in self.rows(task_id)]
        entered, release = threading.Event(), threading.Event()
        sent, errors = [], []

        def fake_send(row):
            sent.append(row["id"])
            if row["id"] == first_id:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("First POST was not released")
            return True, "fake-message-id", None, "accepted"

        def worker(epoch):
            try:
                warmup.process_due_tasks(2, now_epoch=epoch)
            except Exception as exc:
                errors.append(exc)

        with patch.object(services, "_send_via_sendgrid", side_effect=fake_send):
            first_worker = threading.Thread(target=worker, args=(START_EPOCH + 1,), daemon=True)
            first_worker.start()
            try:
                self.assertTrue(entered.wait(5), "First worker did not enter mocked POST")
                # Two due messages, and even the first claim's initial 1-second
                # throttle has elapsed. The active POST must still block worker 2.
                second_worker = threading.Thread(target=worker, args=(START_EPOCH + 2,), daemon=True)
                second_worker.start()
                second_worker.join(5)
                self.assertFalse(second_worker.is_alive())
                self.assertEqual(sent, [first_id])
                self.assertEqual(
                    {row["id"]: row["status"] for row in self.rows(task_id)},
                    {first_id: "sending", second_id: "pending"},
                )
            finally:
                release.set()
                first_worker.join(5)

        self.assertFalse(first_worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sent, [first_id])

    def test_capacity_full_does_not_roll_back_expired_day_cleanup(self):
        list_id = self.add_list("old@example.com", "today@example.com")
        task_id = self.create_task([1, 1], lists=[list_id])
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        old, today = self.rows(task_id)
        execute("UPDATE send_channels SET daily_limit=1 WHERE id=?", (self.channel,))
        day_two_date = datetime.fromtimestamp(START_EPOCH + DAY + 1).date().isoformat()
        execute(
            """INSERT INTO channel_daily_stats
               (channel_id, date, sent_count, failed_count, reserved_count)
               VALUES (?, ?, 1, 0, 0)""",
            (self.channel, day_two_date),
        )

        with patch.object(services, "_send_via_sendgrid") as sender:
            warmup.process_due_tasks(2, now_epoch=START_EPOCH + DAY + 1)
            sender.assert_not_called()

        status_by_id = {row["id"]: row["status"] for row in self.rows(task_id)}
        self.assertEqual(status_by_id, {old["id"]: "missed", today["id"]: "pending"})
        self.assertEqual(
            q_one("SELECT status FROM recipient_pool WHERE id=?", (old["recipient_pool_id"],))["status"],
            "available",
        )

    def test_503_and_429_are_held_for_review_with_recipient_reserved(self):
        for index, http_status in enumerate((503, 429)):
            with self.subTest(http_status=http_status):
                email = "error-{}@example.com".format(http_status)
                list_id = self.add_list(email)
                task_id = self.create_task([1], lists=[list_id])
                epoch = START_EPOCH + index * 10
                warmup.start_task(task_id, now_epoch=epoch)

                def fake_send(row):
                    execute(
                        """INSERT INTO send_log
                           (scheduled_task_id, task_id, channel_id, recipient_email,
                            http_status, status, created_at)
                           VALUES (?, ?, ?, ?, ?, 'failed', ?)""",
                        (row["id"], row["task_id"], row["channel_id"],
                         row["recipient_email"], http_status, services.now_iso()),
                    )
                    return False, None, "HTTP {} from SendGrid".format(http_status), "upstream error body"

                with patch.object(services, "_send_via_sendgrid", side_effect=fake_send) as sender:
                    warmup.process_due_tasks(1, now_epoch=epoch)
                    warmup.process_due_tasks(1, now_epoch=epoch + 1)
                    self.assertEqual(sender.call_count, 1)

                row = self.rows(task_id)[0]
                self.assertEqual(row["status"], "needs_review")
                self.assertEqual(
                    q_one("SELECT status, reserved_task_id FROM recipient_pool WHERE id=?",
                          (row["recipient_pool_id"],)),
                    {"status": "reserved", "reserved_task_id": task_id},
                )

        self.assertEqual(
            q_one("SELECT SUM(reserved_count) AS n FROM channel_daily_stats WHERE channel_id=?",
                  (self.channel,))["n"],
            2,
        )

    def test_legacy_plan_cannot_reuse_address_reserved_by_new_warmup(self):
        named_list = self.add_list("shared@example.com")
        warmup_task = self.create_task([1], lists=[named_list])
        warmup.start_task(warmup_task, now_epoch=START_EPOCH)
        self.add_pool(
            services.POOL_0_3,
            "shared@example.com", "early-1@example.com",
            "early-2@example.com", "early-3@example.com",
        )
        self.add_pool(
            services.POOL_4_30,
            *("late-{}@example.com".format(index) for index in range(27)),
        )
        legacy_channel = services.create_channel(
            self.tag, "legacy-channel", "SG.legacy-key", "legacy@example.com",
            "Legacy Sender", None, 1,
        )
        legacy_task = services.create_mail_task(
            self.tag, legacy_channel, "legacy-plan", "Hello", self.group
        )
        with patch.object(services, "_build_30_day_warmup_limits", return_value={day: 1 for day in range(30)}):
            result = services.generate_plan(legacy_task)
        legacy_addresses = {
            row["recipient_email"].lower()
            for row in q_all("SELECT recipient_email FROM scheduled_email_tasks WHERE task_id=?", (legacy_task,))
        }
        self.assertEqual(result["created"], 30)
        self.assertNotIn("shared@example.com", legacy_addresses)
        self.assertEqual(len(legacy_addresses), 30)

    def test_two_tasks_share_channel_calendar_limit_at_plan_and_send_time(self):
        first = self.add_list("first@example.com")
        second = self.add_list("second@example.com")
        task_one = self.create_task([1], lists=[first])
        task_two = self.create_task([1], lists=[second])
        execute("UPDATE send_channels SET daily_limit=1 WHERE id=?", (self.channel,))
        warmup.start_task(task_one, now_epoch=START_EPOCH)
        with self.assertRaises(ValueError):
            warmup.start_task(task_two, now_epoch=START_EPOCH)
        self.assertEqual(self.rows(task_two), [])

        # A lower limit after both plans exist must still be enforced at send.
        execute("UPDATE send_channels SET daily_limit=2 WHERE id=?", (self.channel,))
        warmup.start_task(task_two, now_epoch=START_EPOCH)
        execute("UPDATE send_channels SET daily_limit=1 WHERE id=?", (self.channel,))
        with patch.object(
            services, "_send_via_sendgrid",
            return_value=(True, "fake-message-id", None, "accepted"),
        ) as sender:
            warmup.process_due_tasks(10, now_epoch=START_EPOCH)
            self.assertEqual(sender.call_count, 1)
        all_statuses = [row["status"] for task in (task_one, task_two) for row in self.rows(task)]
        self.assertEqual(sorted(all_statuses), ["pending", "sent"])
        self.assertEqual(
            q_one("SELECT SUM(sent_count) AS n FROM channel_daily_stats WHERE channel_id=?", (self.channel,))["n"],
            1,
        )

    def test_disabling_selected_named_list_blocks_pending_message(self):
        list_id = self.add_list("one@example.com")
        task_id = self.create_task([1], lists=[list_id])
        warmup.start_task(task_id, now_epoch=START_EPOCH)
        execute("UPDATE recipient_lists SET status='inactive' WHERE id=?", (list_id,))
        with patch.object(services, "_send_via_sendgrid") as sender:
            warmup.process_due_tasks(10, now_epoch=START_EPOCH)
            sender.assert_not_called()
        self.assertEqual(self.rows(task_id)[0]["status"], "missed")
        self.assertEqual(
            q_one("SELECT status FROM recipient_pool WHERE email=?", ("one@example.com",))["status"],
            "available",
        )


if __name__ == "__main__":
    unittest.main()
