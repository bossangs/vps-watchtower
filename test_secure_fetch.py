import unittest
from unittest.mock import Mock, patch
import secure_fetch as sf


class ConnectorSecurityTests(unittest.TestCase):
    def test_local_urls_and_credentials_rejected(self):
        for url in (
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "http://169.254.169.254/",
            "http://localhost/",
            "file:///etc/passwd",
            "https://user:password@example.com/",
            "http://example.com:22/",
            "http://[::ffff:127.0.0.1]/",
            "http://[2002:7f00:1::]/",
            "http://example.com\\@127.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                sf.safe_url(url)

    def test_dns_rejects_mixed_private_answers(self):
        rows = [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("192.168.1.1", 443))]
        with patch.object(
            sf.socket, "getaddrinfo", return_value=rows
        ), self.assertRaises(ValueError):
            sf.resolve_public("example.com", 443)

    def response(self, status=200, body=b"<html>hello</html>", headers=None):
        r = Mock(status=status)
        headers = headers or {}
        r.getheader.side_effect = lambda key, default=None: headers.get(key, default)
        r.read.side_effect = [body, b""]
        return r

    def test_tls_host_original_socket_pinned(self):
        conn = Mock()
        conn.getresponse.return_value = self.response()
        with patch.object(sf, "resolve_public", return_value="8.8.8.8"), patch.object(
            sf.http.client, "HTTPSConnection", return_value=conn
        ) as factory, patch.object(sf.socket, "create_connection") as create:
            self.assertIn(
                "hello", sf.fetch_html("https://example.com/x", 10, 1024, "test")
            )
            conn._create_connection(("example.com", 443), timeout=10)
        self.assertEqual(factory.call_args.args[0], "example.com")
        self.assertEqual(create.call_args.args[0], ("8.8.8.8", 443))
        conn.close.assert_called_once()

    def test_redirect_to_private_is_not_followed(self):
        conn = Mock()
        conn.getresponse.return_value = self.response(
            302, headers={"Location": "http://127.0.0.1/"}
        )
        with patch.object(sf, "resolve_public", return_value="8.8.8.8"), patch.object(
            sf.http.client, "HTTPSConnection", return_value=conn
        ) as factory, self.assertRaises(ValueError):
            sf.fetch_html("https://example.com/", 10, 1024, "test")
        self.assertEqual(factory.call_count, 1)

    def test_403_does_not_read_body_or_retry(self):
        conn = Mock()
        response = self.response(403)
        conn.getresponse.return_value = response
        with patch.object(sf, "resolve_public", return_value="8.8.8.8"), patch.object(
            sf.http.client, "HTTPSConnection", return_value=conn
        ) as factory, self.assertRaisesRegex(ValueError, "403"):
            sf.fetch_html("https://example.com/", 10, 1024, "test")
        response.read.assert_not_called()
        self.assertEqual(factory.call_count, 1)

    def test_challenge_and_large_body_rejected(self):
        for body, limit in [
            (b'<div class="g-recaptcha">captcha</div>', 1024),
            (b"x" * 1025, 1024),
        ]:
            conn = Mock()
            conn.getresponse.return_value = self.response(body=body)
            with patch.object(
                sf, "resolve_public", return_value="8.8.8.8"
            ), patch.object(
                sf.http.client, "HTTPSConnection", return_value=conn
            ), self.assertRaises(
                ValueError
            ):
                sf.fetch_html("https://example.com/", 10, limit, "test")


if __name__ == "__main__":
    unittest.main()
