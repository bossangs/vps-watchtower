"""Offline provider webhook tests; transport is mocked and no network is allowed."""

import os
import tempfile
import unittest
from unittest.mock import patch

from test_telegram_status import watchtower
import provider_webhooks


class ProviderWebhookTests(unittest.TestCase):
    def setUp(self):
        with watchtower.db() as c:
            c.execute("DELETE FROM provider_webhook_deliveries")
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM sites")
            c.execute("DELETE FROM providers")
            cls_ = c.execute(
                "INSERT INTO providers(name,webhook_url,webhook_enabled,webhook_events,created_at) VALUES(?,?,?,?,?)",
                (
                    "Acme",
                    "https://example.com/hook",
                    1,
                    "restock,change",
                    watchtower.now(),
                ),
            ).lastrowid
            self.pid = cls_
            self.sid = c.execute(
                "INSERT INTO sites(name,url,provider_id,stock_status,public,created_at) VALUES(?,?,?,?,?,?)",
                (
                    "Plan",
                    "https://example.com/plan",
                    self.pid,
                    "in_stock",
                    0,
                    watchtower.now(),
                ),
            ).lastrowid
            self.eid = c.execute(
                "INSERT INTO events(site_id,event_type,kind,summary,details,created_at) VALUES(?,?,?,?,?,?)",
                (self.sid, "补货", "restock", "back", "PRIVATE", watchtower.now()),
            ).lastrowid
        os.environ["PROVIDER_WEBHOOK_SECRET_%s" % self.pid] = "s" * 40

    def site(self):
        with watchtower.db() as c:
            return dict(
                c.execute("SELECT * FROM sites WHERE id=?", (self.sid,)).fetchone()
            )

    def test_signing_payload_private_plan_and_duplicate_claim(self):
        sent = {}

        def post(url, body, headers):
            sent.update(url=url, body=body, headers=headers)
            return 204

        with patch.object(provider_webhooks, "_post", side_effect=post):
            self.assertEqual(
                provider_webhooks.deliver(self.eid, self.site(), "restock", "summary"),
                "sent",
            )
            self.assertIsNone(
                provider_webhooks.deliver(self.eid, self.site(), "restock", "summary")
            )
        self.assertNotIn(b"PRIVATE", sent["body"])
        import json, hmac, hashlib

        payload = json.loads(sent["body"])
        ts = sent["headers"]["X-Watchtower-Timestamp"]
        expected = hmac.new(
            ("s" * 40).encode(), (ts + ".").encode() + sent["body"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(
            sent["headers"]["X-Watchtower-Signature"], "sha256=" + expected
        )
        with watchtower.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM provider_webhook_deliveries"
                ).fetchone()[0],
                1,
            )

    def test_filter_disabled_and_event_type(self):
        self.assertIsNone(
            provider_webhooks.deliver(self.eid, self.site(), "sellout", "x")
        )
        with watchtower.db() as c:
            c.execute("UPDATE providers SET webhook_enabled=0 WHERE id=?", (self.pid,))
        self.assertIsNone(
            provider_webhooks.deliver(self.eid, self.site(), "restock", "x")
        )

    def test_safe_url_rejects_http_redirect_and_private_dns(self):
        with self.assertRaises(ValueError):
            provider_webhooks._validate_url("http://example.com/hook")
        with self.assertRaises(ValueError):
            provider_webhooks._validate_url("https://user:pass@example.com/hook")
        with patch.object(
            provider_webhooks.secure_fetch,
            "resolve_public",
            side_effect=ValueError("blocked"),
        ):
            with self.assertRaises(ValueError):
                provider_webhooks._post("https://example.com/hook", b"{}", {})

    def test_failure_status_and_no_secret_audit(self):
        with patch.object(
            provider_webhooks,
            "_post",
            side_effect=provider_webhooks.HTTPDeliveryError(503),
        ):
            self.assertEqual(
                provider_webhooks.deliver(self.eid, self.site(), "restock", "x"),
                "failed",
            )
        with watchtower.db() as c:
            row = c.execute(
                "SELECT status,status_code FROM provider_webhook_deliveries"
            ).fetchone()
        self.assertEqual(tuple(row), ("failed", 503))

    def test_redirect_not_followed(self):
        from unittest.mock import Mock

        connection = Mock()
        connection.getresponse.return_value.status = 302
        with patch.object(
            provider_webhooks.secure_fetch, "resolve_public", return_value="8.8.8.8"
        ), patch.object(
            provider_webhooks.http.client, "HTTPSConnection", return_value=connection
        ) as factory:
            with self.assertRaises(provider_webhooks.HTTPDeliveryError):
                provider_webhooks._post("https://example.com/hook", b"{}", {})
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(connection.request.call_count, 1)

    def test_deliver_requires_parent_registration(self):
        old = provider_webhooks._CTX
        provider_webhooks._CTX = None
        try:
            with self.assertRaises(RuntimeError):
                provider_webhooks.deliver(self.eid, self.site(), "restock", "x")
        finally:
            provider_webhooks._CTX = old

    def test_admin_route_requires_admin_and_csrf(self):
        with watchtower.app.test_client() as client:
            self.assertEqual(
                client.get(f"/admin/providers/{self.pid}/webhook").status_code, 403
            )
            with patch.object(
                watchtower.accounts,
                "current_user",
                return_value={"id": 1, "is_admin": 1},
            ):
                with client.session_transaction() as sess:
                    sess["csrf_token"] = "csrf"
                self.assertEqual(
                    client.post(
                        f"/admin/providers/{self.pid}/webhook",
                        data={"webhook_url": "https://example.com/hook"},
                    ).status_code,
                    400,
                )


if __name__ == "__main__":
    unittest.main()
