"""Focused offline regressions for scheduler rounds and notification safety."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

# Reuse the existing import/temp-database/scheduler-disabled setup.
from test_telegram_status import watchtower


class RegressionTests(unittest.TestCase):
    def setUp(self):
        with watchtower.db() as c:
            c.execute("DELETE FROM notifications")
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM snapshots")
            c.execute("DELETE FROM sites")
        self.addCleanup(patch.stopall)
        self.http_get = patch.object(
            watchtower.requests,
            "get",
            side_effect=AssertionError("real HTTP forbidden"),
        )
        self.http_get.start()
        self.telegram = patch.object(
            watchtower,
            "send_telegram",
            side_effect=AssertionError("real Telegram forbidden"),
        )
        self.telegram.start()

    def add_site(self, name, *, channels="telegram", checked=None, interval=60):
        with watchtower.db() as c:
            return c.execute(
                """INSERT INTO sites
                   (name,url,interval_seconds,channels,last_checked,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    name,
                    "https://example.test/" + name,
                    interval,
                    channels,
                    checked,
                    watchtower.now(),
                ),
            ).lastrowid

    def test_due_round_creates_one_logical_summary_for_expired_sites(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        first, second = self.add_site("expired-a", checked=old), self.add_site(
            "expired-b", checked=old
        )
        with patch.object(
            watchtower, "fetch_site", side_effect=["a", "b"]
        ), patch.object(watchtower, "send_telegram") as send:
            watchtower.due_checks()
        self.assertEqual(send.call_count, 1)
        with watchtower.db() as c:
            summaries = c.execute(
                "SELECT event_type,site_id FROM events WHERE event_type='全站状态汇总'"
            ).fetchall()
        self.assertEqual(len(summaries), 1)
        self.assertEqual({summaries[0]["site_id"]}, {first})
        self.assertNotEqual(
            first, second
        )  # both expired, one report event represents the round

    def test_partial_due_round_marks_latest_site_as_not_checked(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        due = self.add_site("due", checked=old)
        latest = self.add_site("latest", checked=watchtower.now())
        with patch.object(
            watchtower, "fetch_site", return_value="baseline"
        ), patch.object(watchtower, "send_telegram") as send:
            watchtower.due_checks()
        report = send.call_args.args[0]
        self.assertIn("due", report)
        self.assertIn("本轮已检查：首次建立基线", report)
        self.assertIn("latest", report)
        self.assertIn("最近状态（本轮未检查）", report)
        with watchtower.db() as c:
            self.assertEqual(c.execute("SELECT 1").fetchone()[0], 1)

    def test_fetch_site_403_does_not_retry_or_read_body_and_other_site_continues(self):
        first = self.add_site("forbidden")
        second = self.add_site("continues")
        with patch.object(
            watchtower,
            "fetch_html",
            side_effect=[ValueError("HTTP 403 Forbidden"), "<html>still good</html>"],
        ) as fetch, patch.object(watchtower, "send_telegram") as send:
            watchtower.run_checks([first, second])
        self.assertEqual(fetch.call_count, 2)
        with watchtower.db() as c:
            rows = {
                r["id"]: r
                for r in c.execute(
                    "SELECT id,last_status,last_error FROM sites WHERE id IN (?,?)",
                    (first, second),
                )
            }
        self.assertEqual(rows[first]["last_status"], "ERROR")
        self.assertIn("403 Forbidden", rows[first]["last_error"])
        self.assertEqual(rows[second]["last_status"], "OK")
        self.assertEqual(send.call_count, 1)

    def test_failed_check_recovers_on_next_round(self):
        site = self.add_site("recover")
        with patch.object(
            watchtower, "fetch_site", side_effect=[RuntimeError("temporary"), "healthy"]
        ):
            self.assertEqual(watchtower.check_one(site), "检查失败")
            with watchtower.db() as c:
                self.assertEqual(
                    c.execute(
                        "SELECT last_status FROM sites WHERE id=?", (site,)
                    ).fetchone()[0],
                    "ERROR",
                )
            self.assertEqual(watchtower.check_one(site), "首次建立基线")
        with watchtower.db() as c:
            row = c.execute(
                "SELECT last_status,last_error FROM sites WHERE id=?", (site,)
            ).fetchone()
        self.assertEqual(row["last_status"], "OK")
        self.assertEqual(row["last_error"], "")

    def test_unauthorized_manual_check_does_not_check(self):
        site = self.add_site("private")
        with patch.object(watchtower, "fetch_site") as fetch, patch.object(
            watchtower, "send_telegram"
        ) as send:
            with watchtower.app.test_client() as client:
                client.get("/login")
                with client.session_transaction() as sess:
                    token = sess["csrf_token"]
                response = client.post(
                    f"/sites/{site}/check", data={"csrf_token": token}
                )
        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_telegram_network_failure_is_audited_without_token_and_site_stays_ok(self):
        site = self.add_site("safe", channels="telegram")
        token = "super-secret-token"
        with patch.object(
            watchtower, "fetch_site", return_value="safe content"
        ), patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "chat"}
        ), patch.object(
            watchtower.requests,
            "post",
            side_effect=RuntimeError("network failed " + token),
        ):
            watchtower.run_checks([site])
        with watchtower.db() as c:
            row = c.execute(
                "SELECT last_status,last_error FROM sites WHERE id=?", (site,)
            ).fetchone()
            note = c.execute(
                "SELECT status,detail FROM notifications ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["last_status"], "OK")
        self.assertEqual(row["last_error"], "")
        self.assertEqual(note["status"], "failed")
        self.assertNotIn(token, note["detail"])
        self.assertNotIn("api.telegram.org", note["detail"])


if __name__ == "__main__":
    unittest.main()
