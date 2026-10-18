import secrets
import unittest
from unittest.mock import patch
from test_telegram_status import watchtower as w


class UserDeliveryTests(unittest.TestCase):
    def setUp(self):
        with w.db() as c:
            for table in (
                "user_notifications",
                "user_event_claims",
                "subscriptions",
                "telegram_prefs",
                "sessions",
                "users",
                "sites",
                "providers",
            ):
                c.execute("DELETE FROM " + table)
            self.uid = []
            for name, chat in [("user_a", "10001"), ("user_b", "10002")]:
                uid = c.execute(
                    "INSERT INTO users(username,password_hash,active,is_admin,created_at) VALUES(?,?,?,?,?)",
                    (name, "unused", 1, 0, w.now()),
                ).lastrowid
                self.uid.append(uid)
                c.execute(
                    "INSERT INTO telegram_prefs(user_id,chat_id,verified,updated_at) VALUES(?,?,1,?)",
                    (uid, chat, w.now()),
                )
            self.pid = c.execute(
                "INSERT INTO providers(name,push_url,created_at) VALUES('provider','https://example.com/buy',?)",
                (w.now(),),
            ).lastrowid
            self.sites = []
            for name, pid in [
                ("first", self.pid),
                ("second", None),
                ("notchecked", self.pid),
            ]:
                sid = c.execute(
                    "INSERT INTO sites(name,url,provider_id,public,enabled,last_status,created_at) VALUES(?,?,?,1,1,?,?)",
                    (name, "https://example.com/" + name, pid, "OK", w.now()),
                ).lastrowid
                self.sites.append(sid)
            self.eid = c.execute(
                "INSERT INTO events(site_id,event_type,kind,summary,details,created_at) VALUES(?,'套餐补货','restock','back','PRIVATE PAGE SECRET',?)",
                (self.sites[0], w.now()),
            ).lastrowid
        self.sub(self.uid[0], "provider", self.pid)
        self.sub(self.uid[0], "plan", self.sites[0])
        self.sub(self.uid[1], "plan", self.sites[1])
        self.mock = patch.object(w.accounts, "_send_shared")
        self.send = self.mock.start()
        self.addCleanup(self.mock.stop)

    def sub(self, user, typ, target):
        with w.db() as c:
            return c.execute(
                "INSERT INTO subscriptions(user_id,target_type,target_id,created_at) VALUES(?,?,?,?)",
                (user, typ, str(target), w.now()),
            ).lastrowid

    def site(self, idx=0):
        with w.db() as c:
            return dict(
                c.execute(
                    "SELECT * FROM sites WHERE id=?", (self.sites[idx],)
                ).fetchone()
            )

    def test_event_scope_dedup_public_link_and_no_snapshot(self):
        for _ in range(2):
            w.accounts.deliver_event(
                self.eid, self.site(), "restock", "back", "PRIVATE PAGE SECRET"
            )
        self.send.assert_called_once()
        message, chat = self.send.call_args.args
        self.assertEqual(chat, "10001")
        self.assertIn("https://example.com/buy", message)
        self.assertNotIn("PRIVATE", message)

    def test_personal_event_preferences_and_account_active(self):
        with w.db() as c:
            c.execute(
                "UPDATE telegram_prefs SET restock=0 WHERE user_id=?", (self.uid[0],)
            )
        w.accounts.deliver_event(self.eid, self.site(), "restock", "back", "secret")
        self.send.assert_not_called()
        with w.db() as c:
            c.execute(
                "UPDATE telegram_prefs SET restock=1 WHERE user_id=?", (self.uid[0],)
            )
            c.execute("UPDATE users SET active=0 WHERE id=?", (self.uid[0],))
        w.accounts.deliver_event(self.eid, self.site(), "restock", "back", "secret")
        self.send.assert_not_called()

    def test_summary_only_own_scope_and_unchanged_latest(self):
        w.accounts.deliver_status({self.sites[0]: "内容未变"})
        sent = {call.args[1]: call.args[0] for call in self.send.call_args_list}
        self.assertEqual(set(sent), {"10001", "10002"})
        self.assertIn("first", sent["10001"])
        self.assertIn("notchecked", sent["10001"])
        self.assertNotIn("second", sent["10001"])
        self.assertNotIn("first", sent["10002"])
        self.assertIn("本轮未检查", sent["10001"])
        self.send.reset_mock()
        with w.db() as c:
            c.execute("UPDATE telegram_prefs SET status_summary=0")
        w.accounts.deliver_status({self.sites[0]: "内容未变"})
        self.send.assert_not_called()

    def test_unpublished_or_paused_does_not_send(self):
        stale = self.site()
        with w.db() as c:
            c.execute("UPDATE sites SET public=0 WHERE id=?", (self.sites[0],))
        w.accounts.deliver_event(self.eid, stale, "restock", "back", "secret")
        self.send.assert_not_called()
        with w.db() as c:
            c.execute("UPDATE subscriptions SET enabled=0")
        w.accounts.deliver_status({self.sites[0]: "x"})
        self.send.assert_not_called()

    def client_as(self, uid):
        token = secrets.token_urlsafe(32)
        with w.db() as c:
            c.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?)",
                (
                    w.accounts._hash(token),
                    uid,
                    w.now(),
                    w.now(),
                    w.accounts._future(3600),
                ),
            )
        client = w.app.test_client()
        with client.session_transaction() as sess:
            sess["session_token"] = token
            sess["csrf_token"] = "fixture"
        return client

    def test_subscription_update_is_owner_scoped(self):
        with w.db() as c:
            sid = c.execute(
                "SELECT id FROM subscriptions WHERE user_id=?", (self.uid[1],)
            ).fetchone()[0]
        client = self.client_as(self.uid[0])
        self.assertEqual(
            client.post(
                "/subscriptions/" + str(sid),
                data={"enabled": "0", "csrf_token": "fixture"},
            ).status_code,
            302,
        )
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT enabled FROM subscriptions WHERE id=?", (sid,)
                ).fetchone()[0],
                1,
            )

    def test_binding_success_repeated_verify_is_safe(self):
        code = "456789"
        with w.db() as c:
            c.execute(
                "UPDATE telegram_prefs SET verified=0,verify_hash=?,verify_expires=?,verify_attempts=0 WHERE user_id=?",
                (w.accounts._code_hash(code), w.accounts._future(600), self.uid[0]),
            )
        client = self.client_as(self.uid[0])
        for _ in range(2):
            self.assertEqual(
                client.post(
                    "/account/telegram/verify",
                    data={"code": code, "csrf_token": "fixture"},
                ).status_code,
                302,
            )
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT verified FROM telegram_prefs WHERE user_id=?",
                    (self.uid[0],),
                ).fetchone()[0],
                1,
            )

    def test_admin_pages_and_two_url_isolation(self):
        with w.db() as c:
            c.execute("UPDATE users SET is_admin=1 WHERE id=?", (self.uid[0],))
            c.execute(
                "UPDATE providers SET webhook_url='https://example.com/private-hook-secret' WHERE id=?",
                (self.pid,),
            )
        client = self.client_as(self.uid[0])
        for path in [
            "/admin",
            "/admin/providers",
            "/admin/plans/" + str(self.sites[0]),
            "/events",
            "/settings",
            "/admin/sites/" + str(self.sites[0]) + "/snapshots",
            "/admin/providers/" + str(self.pid) + "/webhook",
        ]:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)
        response = client.post(
            "/admin/providers/" + str(self.pid) + "/edit",
            data={
                "csrf_token": "fixture",
                "name": "merchant edited",
                "website": "https://example.com/",
                "description": "intro",
                "push_url": "https://example.com/custom-buy",
            },
        )
        self.assertEqual(response.status_code, 302)
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT push_url FROM providers WHERE id=?", (self.pid,)
                ).fetchone()[0],
                "https://example.com/custom-buy",
            )
        for path in [
            "/providers",
            "/providers/" + str(self.pid),
            "/plans/" + str(self.sites[0]),
        ]:
            text = client.get(path).get_data(as_text=True)
            self.assertNotIn("private-hook-secret", text)
        w.accounts.deliver_event(self.eid, self.site(), "restock", "back", "private")
        self.assertIn("https://example.com/custom-buy", self.send.call_args.args[0])

    def test_detail_subscription_forms_work(self):
        client = self.client_as(self.uid[1])
        path = "/plans/" + str(self.sites[0])
        text = client.get(path).get_data(as_text=True)
        self.assertIn("/subscriptions/toggle", text)
        response = client.post(
            "/subscriptions/toggle",
            data={
                "csrf_token": "fixture",
                "target_type": "plan",
                "target_id": str(self.sites[0]),
            },
        )
        self.assertEqual(response.status_code, 302)
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) FROM subscriptions WHERE user_id=? AND target_type='plan' AND target_id=?",
                    (self.uid[1], str(self.sites[0])),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
