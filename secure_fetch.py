"""Small HTTP connector: public addresses only, DNS pinned per hop, no proxy/cookies/auth."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit, urljoin


def safe_url(raw):
    raw = (raw or "").strip()
    if len(raw) > 2048 or any(ord(c) < 33 for c in raw) or "\\" in raw:
        raise ValueError("网址格式或长度不合法")
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        raise ValueError("网址端口不合法") from None
    if (
        p.scheme not in ("http", "https")
        or not p.hostname
        or p.username is not None
        or p.password is not None
    ):
        raise ValueError("仅允许无账号密码的 HTTP/HTTPS 网址")
    if port not in (None, 80, 443):
        raise ValueError("监控仅允许 80/443 端口")
    host = p.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("禁止本地网络地址")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not public_ip(addr):
            raise ValueError("禁止非公网地址")
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def public_ip(addr):
    return addr.is_global and not (
        addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or getattr(addr, "ipv4_mapped", None)
        or getattr(addr, "sixtofour", None)
        or getattr(addr, "teredo", None)
    )


def resolve_public(host, port):
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("监控域名解析失败") from None
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses or any(
        not public_ip(ipaddress.ip_address(ip)) for ip in addresses
    ):
        raise ValueError("解析结果包含非公网地址，已阻止请求")
    return addresses[0]


def fetch_html(raw, timeout, max_bytes, user_agent):
    url = safe_url(raw)
    for hop in range(6):
        p = urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        ip = resolve_public(p.hostname, port)
        cls = (
            http.client.HTTPSConnection
            if p.scheme == "https"
            else http.client.HTTPConnection
        )
        kwargs = {"timeout": timeout}
        if p.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        conn = cls(p.hostname, port, **kwargs)
        # HTTPConnection still uses the original hostname for Host and TLS SNI/certificate checks.
        # Only the actual socket target is pinned, preventing a second DNS lookup/rebinding.
        conn._create_connection = lambda address, timeout=timeout, source_address=None, **kw: socket.create_connection(
            (ip, port), timeout, source_address
        )
        try:
            conn.request(
                "GET",
                urlunsplit(("", "", p.path or "/", p.query, "")),
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                },
            )
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or hop == 5:
                    raise ValueError("重定向次数超限或目标缺失")
                url = safe_url(urljoin(url, location))
                continue
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status}；不尝试绕过访问限制")
            if response.status != 200:
                raise ValueError(f"不支持的 HTTP 状态 {response.status}")
            content_type = response.getheader("Content-Type", "text/html")
            if not any(
                t in content_type.lower()
                for t in ("text/html", "application/xhtml+xml", "text/plain")
            ):
                raise ValueError("仅支持文本/HTML 响应")
            if response.getheader("Content-Encoding", "identity").lower() not in (
                "identity",
                "",
            ):
                raise ValueError("不接受压缩响应，请使用公开可直接读取的页面")
            chunks, size = [], 0
            while True:
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("响应超过 MAX_RESPONSE_BYTES 限制")
            import re

            m = re.search(r'charset=["\']?([\w-]+)', content_type, re.I)
            encoding = m.group(1) if m else "utf-8"
            try:
                text = b"".join(chunks).decode(encoding, errors="replace")
            except LookupError:
                text = b"".join(chunks).decode("utf-8", errors="replace")
            lowered = text.lower()
            challenges = (
                "cf-chl-",
                "g-recaptcha",
                "hcaptcha",
                "verify you are human",
                "checking your browser",
                "cf-turnstile",
            )
            if any(marker in lowered for marker in challenges):
                raise ValueError("检测到验证码或访问挑战，已停止；不会绕过")
            return text
        except (OSError, http.client.HTTPException):
            raise ValueError("监控连接或 TLS 校验失败") from None
        finally:
            conn.close()
    raise ValueError("重定向次数超限")
