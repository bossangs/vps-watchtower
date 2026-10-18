import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_telegram_status import watchtower as w


class StockIntegrationTests(unittest.TestCase):
    def setUp(self):
        with w.db() as c:
            c.execute("DELETE FROM sites")
            self.sid = c.execute(
                "INSERT INTO sites(name,url,stock_keywords,channels,created_at,public) VALUES(?,?,?,?,?,1)",
                ("fixture", "https://example.com/", "available,有货", "", w.now()),
            ).lastrowid

    def test_baseline_restock_sellout_and_unknown(self):
        with patch.object(
            w,
            "fetch_site",
            side_effect=["sold out", "available", "out of stock", "status pending"],
        ), patch.object(w.accounts, "deliver_event") as event:
            self.assertEqual(w.check_one(self.sid), "首次建立基线")
            w.check_one(self.sid)
            w.check_one(self.sid)
            w.check_one(self.sid)
        self.assertEqual(
            [call.args[2] for call in event.call_args_list],
            ["restock", "sellout", "change"],
        )
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT stock_status FROM sites WHERE id=?", (self.sid,)
                ).fetchone()[0],
                "unknown",
            )
            self.assertEqual(
                [
                    r[0]
                    for r in c.execute(
                        "SELECT kind FROM events WHERE site_id=? ORDER BY id",
                        (self.sid,),
                    )
                ],
                ["restock", "sellout", "change"],
            )

    def test_negative_words_win_and_error_preserves_stock(self):
        self.assertEqual(
            w.stock_state(
                "not available but sold out",
                {"stock_keywords": "available", "soldout_keywords": "sold out"},
            ),
            "out_of_stock",
        )
        self.assertEqual(
            w.stock_state("unavailable", {"stock_keywords": "available"}), "unknown"
        )
        with patch.object(
            w, "fetch_site", side_effect=["available", ValueError("HTTP 403")]
        ):
            w.check_one(self.sid)
            w.check_one(self.sid)
        with w.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT stock_status FROM sites WHERE id=?", (self.sid,)
                ).fetchone()[0],
                "in_stock",
            )

    def test_public_security_headers_and_post_csrf(self):
        client = w.app.test_client()
        response = client.get("/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'none'", response.headers["Content-Security-Policy"])
        for path in (
            "/sites/1/delete",
            "/sites/1/check",
            "/check-all",
            "/logout",
            "/register",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.post(path).status_code, 400)
        self.assertEqual(client.get("/sites/1/toggle").status_code, 405)


class MigrationTests(unittest.TestCase):
    def test_original_schema_data_survives_repeated_upgrade(self):
        # Create precisely the legacy schema, not a new-version database fixture.
        from pathlib import Path

        sql = (Path(__file__).parent / "examples" / "legacy-schema.sql").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            with sqlite3.connect(path) as c:
                c.executescript(sql)
                c.execute(
                    "INSERT INTO sites(name,url,created_at) VALUES('private legacy','https://example.com/','old')"
                )
                c.execute(
                    "INSERT INTO snapshots(site_id,content_hash,content,checked_at) VALUES(1,'original-hash','original snapshot','old')"
                )
                c.execute(
                    "INSERT INTO events(site_id,event_type,summary,details,created_at) VALUES(1,'网页内容变化','old summary','old details','old')"
                )
                c.execute(
                    "INSERT INTO notifications(event_id,channel,status,created_at) VALUES(1,'telegram','sent','old')"
                )
            env = dict(
                os.environ,
                DATABASE_PATH=path,
                DISABLE_SCHEDULER="true",
                APP_SECRET="test-upgrade-secret-" + "x" * 40,
            )
            for _ in range(2):
                subprocess.run(
                    [sys.executable, "-c", "import app"],
                    env=env,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            with sqlite3.connect(path) as c:
                self.assertEqual(
                    c.execute("SELECT name,public,stock_status FROM sites").fetchone(),
                    ("private legacy", 0, "unknown"),
                )
                self.assertEqual(
                    c.execute("SELECT content_hash,content FROM snapshots").fetchone(),
                    ("original-hash", "original snapshot"),
                )
                self.assertEqual(
                    c.execute("SELECT summary,details FROM events").fetchone(),
                    ("old summary", "old details"),
                )
                self.assertEqual(
                    c.execute("SELECT status FROM notifications").fetchone()[0], "sent"
                )
                self.assertEqual(
                    c.execute("SELECT count(*) FROM users").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
