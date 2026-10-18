"""目录扩展离线回归测试；不发起外部网络请求。"""

import math
import unittest
from unittest.mock import patch

# Existing fixture initializes APP_SECRET and a temporary SQLite database.
from test_telegram_status import watchtower


class CatalogTests(unittest.TestCase):
    def setUp(self):
        with watchtower.db() as c:
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM snapshots")
            c.execute("DELETE FROM providers")
            c.execute("DELETE FROM sites")
            c.execute(
                "INSERT INTO sites(name,url,created_at,public,price,currency,billing_period,stock_status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "公开套餐",
                    "https://example.com/a",
                    watchtower.now(),
                    1,
                    5,
                    "USD",
                    "月",
                    "in_stock",
                ),
            )
            self.site = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO sites(name,url,created_at,public,price,currency,billing_period,stock_status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "私有套餐",
                    "https://example.com/b",
                    watchtower.now(),
                    0,
                    1,
                    "CNY",
                    "年",
                    "in_stock",
                ),
            )
            self.private = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO events(site_id,event_type,summary,details,created_at,kind) VALUES(?,?,?,?,?,?)",
                (
                    self.site,
                    "全站状态汇总",
                    "SECRET-SUMMARY",
                    "private",
                    watchtower.now(),
                    "change",
                ),
            )
            c.execute(
                "INSERT INTO events(site_id,event_type,summary,details,created_at,kind) VALUES(?,?,?,?,?,?)",
                (
                    self.site,
                    "库存变化",
                    "公开变化",
                    "details",
                    watchtower.now(),
                    "change",
                ),
            )
        self.client = watchtower.app.test_client()

    def test_private_404_and_summary_isolated(self):
        self.assertEqual(self.client.get(f"/plans/{self.private}").status_code, 404)
        self.assertNotIn(b"SECRET-SUMMARY", self.client.get(f"/plans/{self.site}").data)
        self.assertNotIn(b"SECRET-SUMMARY", self.client.get("/activity").data)

    def test_ssti_is_not_executed(self):
        with watchtower.db() as c:
            c.execute("UPDATE sites SET name=? WHERE id=?", ("{{7*7}}", self.site))
        body = self.client.get("/catalog").data
        self.assertIn(b"{{7*7}}", body)
        self.assertNotIn(b">49<", body)

    def test_catalog_currency_and_period_filter(self):
        self.assertIn(
            "公开套餐".encode(),
            self.client.get(
                "/catalog?currency=USD&billing_period=%E6%9C%88&sort=price_asc"
            ).data,
        )
        self.assertNotIn("私有套餐".encode(), self.client.get("/catalog").data)

    def test_website_validator_rejects_dangerous_and_credentials(self):
        from secure_fetch import safe_url

        for value in (
            "javascript:alert(1)",
            "https://user:pass@example.com/",
            "data:text/html,x",
        ):
            with self.assertRaises(ValueError):
                safe_url(value)

    def test_admin_route_requires_admin(self):
        response = self.client.get("/admin/providers")
        self.assertIn(response.status_code, (302, 401, 403))


if __name__ == "__main__":
    unittest.main()
