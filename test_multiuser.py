import os, unittest
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from test_telegram_status import watchtower


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        self.app = watchtower.app
        self.app.config["TESTING"] = True
        self.app.config["REGISTRATION_ENABLED"] = True
        with watchtower.db() as c:
            c.execute("DELETE FROM users")
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM subscriptions")
            c.execute("DELETE FROM telegram_prefs")
            c.execute("DELETE FROM rate_limits")
            c.execute("DELETE FROM user_notifications")
            c.execute(
                "INSERT INTO users(username,password_hash,active,is_admin,created_at) VALUES(?,?,?,?,?)",
                (
                    "alice",
                    generate_password_hash("correct-password", method="scrypt"),
                    1,
                    0,
                    watchtower.now(),
                ),
            )
        self.client = self.app.test_client()
        self.client.get("/login")

    def token(self):
        with self.client.session_transaction() as s:
            return s.get("csrf_token")

    def test_register_and_csrf(self):
        r = self.client.get("/register")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/register", data={"username": "bob", "password": "long-password"}
            ).status_code,
            400,
        )
        r = self.client.post(
            "/register",
            data={
                "username": "bob",
                "password": "long-password",
                "csrf_token": self.token(),
            },
        )
        self.assertEqual(r.status_code, 302)

    def test_open_redirect_and_login_limit(self):
        with self.client.session_transaction() as s:
            s["csrf_token"] = "x"
        r = self.client.post(
            "/login?next=https://evil.example/",
            data={
                "username": "alice",
                "password": "correct-password",
                "csrf_token": "x",
            },
        )
        self.assertNotIn("evil.example", r.location)
        for _ in range(9):
            last = self.client.post(
                "/login",
                data={
                    "username": "alice",
                    "password": "bad",
                    "csrf_token": self.token(),
                },
            )
        self.assertIn("频繁", last.get_data(as_text=True))

    def test_csrf_and_password_revokes_all_sessions(self):
        r = self.client.post(
            "/login",
            data={
                "username": "alice",
                "password": "correct-password",
                "csrf_token": self.token(),
            },
        )
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as s:
            old = s["session_token"]
            csrf = s["csrf_token"]
        r = self.client.post(
            "/account/password",
            data={
                "current": "correct-password",
                "new": "new-password-123",
                "csrf_token": csrf,
            },
        )
        self.assertEqual(r.status_code, 302)
        with watchtower.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT revoked FROM sessions WHERE token_hash=?",
                    (watchtower.accounts._hash(old),),
                ).fetchone()[0],
                1,
            )

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake"})
    @patch("requests.post")
    def test_telegram_cooldown_and_five_attempts(self, post):
        post.return_value.json.return_value = {"ok": True}
        post.return_value.status_code = 200
        self.client.post(
            "/login",
            data={
                "username": "alice",
                "password": "correct-password",
                "csrf_token": self.token(),
            },
        )
        csrf = self.token()
        self.client.post(
            "/account/telegram", data={"chat_id": "12345", "csrf_token": csrf}
        )
        self.assertEqual(post.call_count, 1)
        self.client.post(
            "/account/telegram", data={"chat_id": "12345", "csrf_token": csrf}
        )
        self.assertEqual(post.call_count, 1)
        for _ in range(6):
            self.client.post(
                "/account/telegram/verify", data={"code": "000000", "csrf_token": csrf}
            )
        with watchtower.db() as c:
            self.assertEqual(
                c.execute("SELECT verified FROM telegram_prefs").fetchone()[0], 0
            )

    def test_private_site_cannot_be_subscribed(self):
        with watchtower.db() as c:
            sid = c.execute(
                "INSERT INTO sites(name,url,public,enabled,created_at) VALUES(?,?,?,?,?)",
                ("private", "https://private", 0, 1, watchtower.now()),
            ).lastrowid
        self.client.post(
            "/login",
            data={
                "username": "alice",
                "password": "correct-password",
                "csrf_token": self.token(),
            },
        )
        with self.client.session_transaction() as s:
            csrf = s["csrf_token"]
        self.assertEqual(
            self.client.post(
                "/subscriptions/subscribe",
                data={"target_type": "plan", "target_id": sid, "csrf_token": csrf},
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
