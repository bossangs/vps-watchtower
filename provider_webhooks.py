"""Per-provider HTTPS webhooks.

Secrets are intentionally environment-only.  Delivery claims are durable: a
process crash after a ``pending`` claim is not retried automatically, so an
operator can inspect the audit row without risking duplicate notifications.
"""

import hashlib
import hmac
import json
import os
import socket
import ssl
import http.client
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from flask import abort, flash, redirect, request, render_template, url_for

import secure_fetch

EVENTS = {"restock", "sellout", "change"}
MAX_RESPONSE = 4096
TIMEOUT = 10
_CTX = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _secret(provider_id):
    return os.getenv(f"PROVIDER_WEBHOOK_SECRET_{int(provider_id)}", "")


def _events(raw):
    return ",".join(x for x in (raw or "").lower().split(",") if x in EVENTS)


def _validate_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    value = secure_fetch.safe_url(raw)
    if urlsplit(value).scheme != "https":
        raise ValueError("商家推送地址仅允许 HTTPS")
    return value


def _migrate(w):
    with w.db() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(providers)")}
        additions = {
            "webhook_url": "TEXT DEFAULT ''",
            "webhook_enabled": "INTEGER NOT NULL DEFAULT 0",
            "webhook_events": "TEXT NOT NULL DEFAULT 'restock,sellout,change'",
        }
        for name, typ in additions.items():
            if name not in cols:
                c.execute(f"ALTER TABLE providers ADD COLUMN {name} {typ}")
        c.execute("""CREATE TABLE IF NOT EXISTS provider_webhook_deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            status_code INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        )""")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_webhook_event ON provider_webhook_deliveries(provider_id,event_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_webhook_created ON provider_webhook_deliveries(provider_id,created_at DESC)"
        )


def register(w, accounts):
    """Install schema and the admin-only configuration/audit route."""
    global _CTX
    _CTX = (w, accounts)
    _migrate(w)
    app = w.app

    @app.route("/admin/providers/<int:provider_id>/webhook", methods=["GET", "POST"])
    @accounts.require_admin
    def admin_provider_webhook(provider_id):
        if request.method == "POST":
            try:
                # app.before_request normally enforces this; retain a local
                # check for direct/unit invocation and future app integrations.
                token = request.form.get("csrf_token") or request.headers.get(
                    "X-CSRF-Token", ""
                )
                expected = accounts.csrf_token()
                if not token or not hmac.compare_digest(str(token), str(expected)):
                    abort(400, description="表单安全令牌无效")
                address = _validate_url(request.form.get("webhook_url", ""))
                enabled = (
                    1
                    if request.form.get("webhook_enabled") in ("1", "on", "true")
                    else 0
                )
                selected = _events(",".join(request.form.getlist("webhook_events")))
                if enabled:
                    if not address:
                        raise ValueError("启用时必须填写 HTTPS 推送地址")
                    if len(_secret(provider_id)) < 32:
                        raise ValueError(
                            f"环境变量 PROVIDER_WEBHOOK_SECRET_{provider_id} 未配置或少于 32 字符"
                        )
                    if not selected:
                        raise ValueError("启用时至少选择一个事件类型")
                with w.db() as c:
                    cur = c.execute(
                        "UPDATE providers SET webhook_url=?,webhook_enabled=?,webhook_events=? WHERE id=?",
                        (
                            address,
                            enabled,
                            selected or "restock,sellout,change",
                            provider_id,
                        ),
                    )
                    if not cur.rowcount:
                        return "商家不存在", 404
                flash("商家 Webhook 配置已保存")
            except ValueError as exc:
                flash(str(exc))
            return redirect(url_for("admin_provider_webhook", provider_id=provider_id))
        with w.db() as c:
            provider = c.execute(
                "SELECT id,name,webhook_url,webhook_enabled,webhook_events FROM providers WHERE id=?",
                (provider_id,),
            ).fetchone()
            if not provider:
                return "商家不存在", 404
            audits = [
                dict(x)
                for x in c.execute(
                    "SELECT id,event_id,status,status_code,created_at FROM provider_webhook_deliveries WHERE provider_id=? ORDER BY id DESC LIMIT 100",
                    (provider_id,),
                )
            ]
        context = dict(provider)
        context["secret_configured"] = len(_secret(provider_id)) >= 32
        rendered = render_template(
            "provider_webhook.html", provider=context, audits=audits
        )
        return w.page(rendered, _rendered=True)


class HTTPDeliveryError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Webhook non-2xx response")


def _post(url, raw_body, headers):
    """POST once to a DNS-pinned public IP while retaining TLS SNI/cert checks."""
    url = _validate_url(url)
    p = urlsplit(url)
    port = p.port or 443
    ip = secure_fetch.resolve_public(p.hostname, port)
    conn = http.client.HTTPSConnection(
        p.hostname, port, timeout=TIMEOUT, context=ssl.create_default_context()
    )
    conn._create_connection = lambda address, timeout=TIMEOUT, source_address=None, **kw: socket.create_connection(
        (ip, port), timeout, source_address
    )
    try:
        conn.request(
            "POST",
            urlunsplit(("", "", p.path or "/", p.query, "")),
            body=raw_body,
            headers=headers,
        )
        response = conn.getresponse()
        response.read(MAX_RESPONSE)
        if not 200 <= response.status < 300:
            raise HTTPDeliveryError(response.status)
        return response.status
    finally:
        conn.close()


def deliver(event_id, site, kind, summary):
    """Claim and send one event for its owning provider, returning audit status."""
    if _CTX is None:
        raise RuntimeError("provider_webhooks.register() must run before deliver()")
    w, _accounts = _CTX
    site = dict(site)
    provider_id = site.get("provider_id")
    if not provider_id or kind not in EVENTS:
        return None
    with w.db() as c:
        provider = c.execute(
            "SELECT id,name,webhook_url,webhook_enabled,webhook_events FROM providers WHERE id=?",
            (provider_id,),
        ).fetchone()
        if (
            not provider
            or not provider["webhook_enabled"]
            or kind not in _events(provider["webhook_events"])
            or not provider["webhook_url"]
        ):
            return None
        try:
            cur = c.execute(
                "INSERT INTO provider_webhook_deliveries(provider_id,event_id,status,status_code,created_at) VALUES(?,?,?,?,?)",
                (provider_id, event_id, "pending", None, _now()),
            )
        except Exception:
            return None  # unique claim: another worker already owns it
        delivery_id = cur.lastrowid
        event = c.execute(
            "SELECT created_at FROM events WHERE id=?", (event_id,)
        ).fetchone()
    secret = _secret(provider_id)
    # Configuration could have been changed after claim; do not send without a valid secret.
    if len(secret) < 32:
        with w.db() as c:
            c.execute(
                "UPDATE provider_webhook_deliveries SET status=?,status_code=? WHERE id=?",
                ("failed", None, delivery_id),
            )
        return "failed"
    plan = {
        "id": int(site.get("id", 0)),
        "name": str(site.get("name", ""))[:500],
        "url": str(site.get("url", ""))[:2048],
        "stock_status": site.get("stock_status", "unknown"),
    }
    payload = {
        "schema_version": 1,
        "event_id": int(event_id),
        "event": kind,
        "occurred_at": (event["created_at"] if event else _now()),
        "provider": {"id": int(provider["id"]), "name": provider["name"]},
        "plan": plan,
        "summary": str(summary or "")[:2000],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), (timestamp + ".").encode() + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "X-Watchtower-Timestamp": timestamp,
        "X-Watchtower-Event": kind,
        "X-Watchtower-Delivery": f"evt-{event_id}-provider-{provider_id}",
        "X-Watchtower-Signature": "sha256=" + signature,
    }
    status, code = "sent", None
    try:
        code = _post(provider["webhook_url"], raw, headers)
    except HTTPDeliveryError as exc:
        status, code = "failed", exc.status
    except Exception:
        status = "failed"
    with w.db() as c:
        c.execute(
            "UPDATE provider_webhook_deliveries SET status=?,status_code=? WHERE id=?",
            (status, code, delivery_id),
        )
    return status
