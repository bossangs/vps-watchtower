"""Offline regression tests; no real HTTP requests or Telegram messages."""
import os
import tempfile
import unittest
from unittest.mock import patch, Mock

_TEMP = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_TEMP.name, "test.db")
with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
    import app as watchtower


class TelegramStatusTests(unittest.TestCase):
    def setUp(self):
        with watchtower.db() as c:
            c.execute("DELETE FROM sites")
        self.network = patch.object(watchtower.requests, "get", side_effect=AssertionError("Real HTTP forbidden"))
        self.network.start()
        self.post = patch.object(watchtower.requests, "post", side_effect=AssertionError("Real Telegram forbidden"))
        self.post.start()
        self.fetch = patch.object(watchtower, "fetch_site", return_value="same content")
        self.fetch_mock = self.fetch.start()
        self.sender = patch.object(watchtower, "send_telegram")
        self.send_mock = self.sender.start()
        self.addCleanup(patch.stopall)

    def add(self, name, channels="telegram", enabled=1, checked=None):
        with watchtower.db() as c:
            return c.execute(
                "INSERT INTO sites(name,url,channels,enabled,last_checked,created_at) VALUES(?,?,?,?,?,?)",
                (name, "https://example.com/" + name, channels, enabled, checked, watchtower.now())
            ).lastrowid

    def report(self):
        return "\n".join(call.args[0] for call in self.send_mock.call_args_list)

    def test_baseline_unchanged_and_full_round(self):
        ids = [self.add(name) for name in ("A", "B", "C")]
        watchtower.run_checks(ids)
        self.assertEqual(self.send_mock.call_count, 1)
        self.assertIn("首次建立基线", self.report())
        for name in ("A", "B", "C"):
            self.assertIn("https://example.com/" + name, self.report())
        self.send_mock.reset_mock()
        watchtower.run_checks(ids)
        self.assertEqual(self.send_mock.call_count, 1)
        self.assertIn("内容未变", self.report())

    def test_error_403_does_not_retry_or_skip_other_sites(self):
        ids = [self.add("VMISS"), self.add("B")]
        self.fetch_mock.side_effect = [RuntimeError("403 Forbidden"), "content"]
        watchtower.run_checks(ids)
        self.assertEqual(self.fetch_mock.call_count, 2)
        self.assertEqual(self.send_mock.call_count, 1)
        self.assertIn("ERROR", self.report())
        self.assertIn("403 Forbidden", self.report())
        self.assertIn("首次建立基线", self.report())

    def test_partial_round_includes_latest_and_never_checked(self):
        a = self.add("A")
        self.add("B", checked="2020-01-01T00:00:00+00:00")
        self.add("C")
        watchtower.run_checks([a])
        self.assertIn("最近状态（本轮未检查）", self.report())
        self.assertIn("2020-01-01", self.report())
        self.assertIn("尚未检查", self.report())

    def test_disabled_and_nontelegram_are_excluded(self):
        a = self.add("A", channels=" TELEGRAM ,telegram,,")
        self.add("PAUSED", enabled=0)
        self.add("EMAIL", channels="email")
        watchtower.run_checks([a])
        self.assertEqual(self.send_mock.call_count, 1)
        self.assertNotIn("PAUSED", self.report())
        self.assertNotIn("EMAIL", self.report())

    def test_no_due_no_report(self):
        self.add("A", checked=watchtower.now())
        watchtower.due_checks()
        self.fetch_mock.assert_not_called()
        self.send_mock.assert_not_called()
        watchtower.run_checks([])
        self.send_mock.assert_not_called()

    def test_no_telegram_no_report(self):
        watchtower.run_checks([self.add("A", channels="")])
        self.send_mock.assert_not_called()

    def test_changed_keeps_event_and_other_channels_without_duplicate_telegram(self):
        a = self.add("A", channels="telegram,email")
        watchtower.run_checks([a])
        self.send_mock.reset_mock()
        self.fetch_mock.return_value = "changed content"
        with patch.dict(watchtower.SENDERS, {"telegram": Mock(), "email": Mock()}) as senders:
            watchtower.run_checks([a])
            senders["telegram"].assert_not_called()
            senders["email"].assert_called_once()
        self.send_mock.assert_called_once()
        self.assertIn("内容变化", self.report())
        with watchtower.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='网页内容变化'").fetchone()[0], 1)

    def test_split_preserves_all_sites_and_failure_is_audited(self):
        ids = [self.add("site" + str(i) + "x" * 155) for i in range(30)]
        watchtower.run_checks(ids)
        self.assertGreater(self.send_mock.call_count, 1)
        self.assertTrue(all(len(call.args[0]) < 4000 for call in self.send_mock.call_args_list))
        for i in range(30):
            self.assertIn("https://example.com/site" + str(i) + "x", self.report())
        self.send_mock.side_effect = RuntimeError("fake secret URL")
        watchtower.run_checks(ids)
        with watchtower.db() as c:
            row = c.execute("SELECT status,detail FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertNotIn("secret", row["detail"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sites WHERE last_status='OK'").fetchone()[0], 30)

    def test_manual_routes_and_lock(self):
        a = self.add("A")
        self.add("B")
        with watchtower.app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            self.assertEqual(client.get(f"/sites/{a}/check").status_code, 302)
            self.assertIn("本轮未检查", self.report())
            self.send_mock.reset_mock()
            self.assertEqual(client.get("/check-all").status_code, 302)
            self.send_mock.assert_called_once()
        self.assertFalse(watchtower.check_lock.locked())
        watchtower.check_lock.acquire()
        try:
            self.send_mock.reset_mock()
            watchtower.due_checks()
            self.send_mock.assert_not_called()
        finally:
            watchtower.check_lock.release()

    def test_http_200_ok_false_is_failure(self):
        self.sender.stop()
        response = Mock()
        response.json.return_value = {"ok": False}
        with patch.dict(os.environ, TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="fake"), \
                patch.object(watchtower.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "ok=false"):
                watchtower.send_telegram("test")


if __name__ == "__main__":
    unittest.main()
