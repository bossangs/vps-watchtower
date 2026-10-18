import hashlib, hmac, os, re, secrets, time
from functools import wraps
from datetime import datetime, timezone
from urllib.parse import urlsplit
from flask import request, session, redirect, url_for, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

MIN_PASSWORD, MAX_PASSWORD = 10, 128
EVENTS = {"restock", "sellout", "change"}


def register(w):
    app = getattr(w, "app", None) or w
    register._w = w
    app.config.setdefault(
        "REGISTRATION_ENABLED",
        os.getenv("REGISTRATION_ENABLED", "false").lower() not in ("0", "false", "no"),
    )
    app.config.setdefault("MAX_USERS", max(1, int(os.getenv("MAX_USERS", "1000"))))
    migrate()
    app.jinja_env.globals.update(csrf_token=csrf_token, current_user=current_user)

    @app.before_request
    def _csrf():
        if request.method == "POST" and (request.endpoint or "").startswith(
            "multiuser_"
        ):
            x = request.form.get("csrf_token") or request.headers.get(
                "X-CSRF-Token", ""
            )
            if not x or not hmac.compare_digest(str(x), str(csrf_token())):
                abort(400, description="Invalid CSRF token")

    def render(body, **kw):
        return w.page(body, **kw)

    @app.route("/register", methods=["GET", "POST"], endpoint="multiuser_register")
    def register_route():
        if request.method == "POST":
            u, p = _username(request.form.get("username")), request.form.get(
                "password", ""
            )
            ip = request.remote_addr or "unknown"
            if _limited("register", ip, "*"):
                flash("注册请求过于频繁，请稍后再试")
            elif not app.config["REGISTRATION_ENABLED"]:
                flash("注册当前已关闭")
            elif not u or not _valid(p):
                flash("用户名无效；密码长度须为 10-128 位")
            else:
                _hit("register", ip, "*")
                with w.db() as c:
                    c.execute("BEGIN IMMEDIATE")
                    n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    exists = c.execute(
                        "SELECT 1 FROM users WHERE username=?", (u,)
                    ).fetchone()
                    if n >= app.config["MAX_USERS"]:
                        flash("注册名额已满")
                    elif exists:
                        flash("用户名已存在")
                    else:
                        uid = c.execute(
                            "INSERT INTO users(username,password_hash,active,is_admin,created_at) VALUES(?,?,?,?,?)",
                            (u, _pw(p), 1, 0, w.now()),
                        ).lastrowid
                if not exists and n < app.config["MAX_USERS"]:
                    _login(uid, True)
                    flash("注册成功")
                    return redirect(url_for("multiuser_account"))
        return render(_form("注册", "/register", "创建账户", register=True))

    @app.route("/login", methods=["GET", "POST"], endpoint="multiuser_login")
    def login_route():
        if request.method == "POST":
            u, p = _username(request.form.get("username")), request.form.get(
                "password", ""
            )
            ip = request.remote_addr or "unknown"
            if (
                _limited("login", ip, u)
                or _limited("login_ip", ip, "*")
                or _limited("login_account", u, "*")
            ):
                flash("登录尝试过于频繁，请稍后再试")
            else:
                _hit("login", ip, u)
                _hit("login_ip", ip, "*")
                _hit("login_account", u, "*")
                with w.db() as c:
                    row = c.execute(
                        "SELECT * FROM users WHERE username=?", (u,)
                    ).fetchone()
                if (
                    row
                    and row["active"]
                    and len(p) <= MAX_PASSWORD
                    and check_password_hash(row["password_hash"], p)
                ):
                    with w.db() as c:
                        c.execute(
                            "DELETE FROM rate_limits WHERE bucket=? AND subject=? AND key=?",
                            ("login", ip, u),
                        )
                    _login(row["id"], True)
                    return redirect(
                        _safe_next(request.args.get("next"))
                        or url_for("multiuser_account")
                    )
                flash("用户名或密码错误")
        return render(_form("登录", "/login", "登录"))

    @app.route("/logout", methods=["POST"], endpoint="multiuser_logout")
    def logout():
        _revoke()
        session.clear()
        return redirect(url_for("multiuser_login"))

    @app.route("/account", endpoint="multiuser_account")
    @require_user
    def account():
        u = current_user()
        with w.db() as c:
            tg = c.execute(
                "SELECT * FROM telegram_prefs WHERE user_id=?", (u["id"],)
            ).fetchone()
        return render(
            "<div class=card><h2>账户：{{u.username}}</h2><p>Telegram：{{tg.chat_id if tg and tg.verified else '未绑定'}}</p><p><a href='{{url_for('multiuser_telegram')}}'>Telegram 设置</a> <a href='{{url_for('multiuser_subscriptions')}}'>订阅管理</a> <a href='{{url_for('multiuser_password')}}'>修改密码</a> <a href='{{url_for('multiuser_notifications')}}'>发送记录</a></p><form method=post action='{{url_for('multiuser_logout')}}'><input type=hidden name=csrf_token value='{{csrf_token()}}'><button>退出</button></form></div>",
            u=u,
            tg=tg,
        )

    @app.route(
        "/account/password", methods=["GET", "POST"], endpoint="multiuser_password"
    )
    @require_user
    def password():
        if request.method == "POST":
            u, old, new = (
                current_user(),
                request.form.get("current", ""),
                request.form.get("new", ""),
            )
            with w.db() as c:
                r = c.execute(
                    "SELECT password_hash FROM users WHERE id=?", (u["id"],)
                ).fetchone()
            if _limited("password", str(u["id"]), "*"):
                flash("密码操作过于频繁，请稍后再试")
                return redirect(url_for("multiuser_password"))
            _hit("password", str(u["id"]), "*")
            if (
                not r
                or len(old) > MAX_PASSWORD
                or not check_password_hash(r[0], old)
                or not _valid(new)
            ):
                flash("当前密码错误或新密码长度无效")
            else:
                with w.db() as c:
                    c.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (_pw(new), u["id"]),
                    )
                    c.execute(
                        "UPDATE sessions SET revoked=1 WHERE user_id=?", (u["id"],)
                    )
                session.clear()
                flash("密码已修改，请重新登录")
                return redirect(url_for("multiuser_login"))
        return render(_form("修改密码", "/account/password", "保存", password=True))

    @app.route(
        "/account/telegram", methods=["GET", "POST"], endpoint="multiuser_telegram"
    )
    @require_user
    def telegram():
        u = current_user()
        if request.method == "POST":
            a = request.form.get("action", "send")
            if a == "unbind":
                with w.db() as c:
                    c.execute("DELETE FROM telegram_prefs WHERE user_id=?", (u["id"],))
                flash("Telegram 已解绑")
            elif a == "prefs":
                vals = [
                    int(request.form.get(x) == "1")
                    for x in ("restock", "sellout", "change", "status_summary")
                ]
                with w.db() as c:
                    c.execute(
                        "UPDATE telegram_prefs SET restock=?,sellout=?,change=?,status_summary=?,updated_at=? WHERE user_id=?",
                        (*vals, w.now(), u["id"]),
                    )
                flash("通知偏好已保存")
            else:
                chat = request.form.get("chat_id", "").strip()
                if not re.fullmatch(r"[0-9]{1,20}", chat) or int(chat) <= 0:
                    flash("仅允许正整数私聊 chat_id")
                elif not os.getenv("TELEGRAM_BOT_TOKEN"):
                    flash("服务器尚未配置共享 Telegram Bot")
                elif _limited("telegram_send", str(u["id"]), "send"):
                    flash("验证码发送冷却中，请稍后再试")
                else:
                    _hit("telegram_send", str(u["id"]), "send")
                    code = f"{secrets.randbelow(1000000):06d}"
                    with w.db() as c:
                        c.execute(
                            "INSERT INTO telegram_prefs(user_id,chat_id,verified,verify_hash,verify_expires,verify_attempts,restock,sellout,change,status_summary,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,verified=0,verify_hash=excluded.verify_hash,verify_expires=excluded.verify_expires,verify_attempts=0,updated_at=excluded.updated_at",
                            (
                                u["id"],
                                chat,
                                0,
                                _code_hash(code),
                                _future(600),
                                0,
                                1,
                                1,
                                1,
                                1,
                                w.now(),
                            ),
                        )
                    try:
                        _send_shared("Watchtower 绑定验证码：" + code, chat)
                        flash("验证码已发送，10分钟内有效")
                    except Exception:
                        flash("验证码发送失败，请稍后重试")
        with w.db() as c:
            tg = c.execute(
                "SELECT * FROM telegram_prefs WHERE user_id=?", (u["id"],)
            ).fetchone()
        return render(
            "<div class=card><h2>Telegram 设置</h2><p>共享 Bot；不需要个人 token。仅支持正整数私聊 chat_id。</p><form method=post><input type=hidden name=csrf_token value='{{csrf_token()}}'><input type=hidden name=action value=send><input name=chat_id required><button>发送验证码</button></form><form method=post action='{{url_for('multiuser_telegram_verify')}}'><input type=hidden name=csrf_token value='{{csrf_token()}}'><input name=code pattern='[0-9]{6}' required><button>验证</button></form>{% if tg and tg.verified %}<form method=post><input type=hidden name=csrf_token value='{{csrf_token()}}'><input type=hidden name=action value=unbind><button>解绑</button></form><form method=post action='{{url_for('multiuser_telegram_test')}}'><input type=hidden name=csrf_token value='{{csrf_token()}}'><button>测试消息</button></form><form method=post><input type=hidden name=csrf_token value='{{csrf_token()}}'><input type=hidden name=action value=prefs>{% for x in ['restock','sellout','change','status_summary'] %}<label><input type=checkbox name={{x}} value=1 {% if tg[x] %}checked{% endif %}>{{ {'restock':'补货提醒','sellout':'售罄提醒','change':'内容变化','status_summary':'订阅状态汇总'}[x] }}</label>{% endfor %}<button>保存偏好</button></form>{% endif %}<a href='{{url_for('multiuser_notifications')}}'>发送记录</a></div>",
            tg=tg,
        )

    @app.route(
        "/account/telegram/verify",
        methods=["POST"],
        endpoint="multiuser_telegram_verify",
    )
    @require_user
    def verify():
        u, code = current_user(), request.form.get("code", "").strip()
        with w.db() as c:
            r = c.execute(
                "SELECT * FROM telegram_prefs WHERE user_id=?", (u["id"],)
            ).fetchone()
        ok = bool(
            r
            and r["verify_hash"]
            and r["verify_attempts"] < 5
            and r["verify_expires"]
            and r["verify_expires"] > w.now()
            and hmac.compare_digest(r["verify_hash"], _code_hash(code))
        )
        with w.db() as c:
            if ok:
                c.execute(
                    "UPDATE telegram_prefs SET verified=1,verify_hash=NULL,verify_expires=NULL,verify_attempts=0,updated_at=? WHERE user_id=?",
                    (w.now(), u["id"]),
                )
            elif r:
                c.execute(
                    "UPDATE telegram_prefs SET verify_attempts=verify_attempts+1,updated_at=? WHERE user_id=?",
                    (w.now(), u["id"]),
                )
        flash("Telegram 已验证" if ok else "验证码错误、已过期或尝试次数已用尽")
        return redirect(url_for("multiuser_telegram"))

    @app.route(
        "/account/telegram/test", methods=["POST"], endpoint="multiuser_telegram_test"
    )
    @require_user
    def telegram_test():
        u = current_user()
        with w.db() as c:
            r = c.execute(
                "SELECT * FROM telegram_prefs WHERE user_id=? AND verified=1",
                (u["id"],),
            ).fetchone()
        if not r:
            flash("请先验证 Telegram")
        elif _limited("telegram_test", str(u["id"]), "*"):
            flash("测试消息发送过于频繁")
        else:
            _hit("telegram_test", str(u["id"]), "*")
            try:
                _send_shared("[VPS Watchtower] 测试消息", r["chat_id"])
                _audit(None, "telegram", "sent", "test", u["id"])
                flash("测试消息已发送")
            except Exception:
                _audit(None, "telegram", "failed", "test", u["id"])
                flash("测试消息发送失败")
        return redirect(url_for("multiuser_telegram"))

    @app.route("/subscriptions", endpoint="multiuser_subscriptions")
    @require_user
    def subscriptions():
        with w.db() as c:
            rows = c.execute(
                "SELECT s.*,CASE WHEN s.target_type='plan' THEN p.name ELSE v.name END target_name,CASE WHEN s.target_type='plan' THEN p.url ELSE v.website END target_url FROM subscriptions s LEFT JOIN sites p ON s.target_type='plan' AND p.id=CAST(s.target_id AS INTEGER) AND p.public=1 LEFT JOIN providers v ON s.target_type='provider' AND v.id=CAST(s.target_id AS INTEGER) WHERE s.user_id=? ORDER BY s.id DESC",
                (current_user()["id"],),
            ).fetchall()
        return render(
            "<div class=card><h2>订阅管理</h2>{% for s in rows %}<form method=post action='{{url_for('multiuser_sub_update',sid=s.id)}}'><input type=hidden name=csrf_token value='{{csrf_token()}}'><a href='{{s.target_url or '#'}}'>{{s.target_name or '已删除'}}</a> <select name=event_types>{% for x in ['restock,sellout,change','restock,sellout','restock','sellout','change'] %}<option value={{x}} {% if s.event_types==x %}selected{% endif %}>{{x}}</option>{% endfor %}</select><label><input type=checkbox name=status_summary value=1 {% if s.status_summary %}checked{% endif %}>汇总</label><label><input type=checkbox name=enabled value=1 {% if s.enabled %}checked{% endif %}>开启</label><input type=hidden name=target_type value='{{s.target_type}}'><input type=hidden name=target_id value='{{s.target_id}}'><button>保存</button> <button class='btn red' formaction='/subscriptions/toggle'>取消订阅</button></form>{% else %}<p>暂无订阅</p>{% endfor %}</div>",
            rows=rows,
        )

    @app.route(
        "/subscriptions/<int:sid>", methods=["POST"], endpoint="multiuser_sub_update"
    )
    @require_user
    def sub_update(sid):
        ev = (
            ",".join(
                x for x in request.form.get("event_types", "").split(",") if x in EVENTS
            )
            or "change"
        )
        with w.db() as c:
            c.execute(
                "UPDATE subscriptions SET event_types=?,status_summary=?,enabled=? WHERE id=? AND user_id=?",
                (
                    ev,
                    int(request.form.get("status_summary") == "1"),
                    int(request.form.get("enabled") == "1"),
                    sid,
                    current_user()["id"],
                ),
            )
        return redirect(url_for("multiuser_subscriptions"))

    @app.route(
        "/subscriptions/toggle", methods=["POST"], endpoint="multiuser_sub_toggle"
    )
    @require_user
    def toggle_subscription():
        typ = request.form.get("target_type", "").lower()
        raw = request.form.get("target_id", "")
        if typ not in ("plan", "provider") or not raw.isdigit() or len(raw) > 12:
            abort(400)
        target = str(int(raw))
        uid = current_user()["id"]
        with w.db() as c:
            query = (
                "SELECT id FROM sites WHERE id=? AND public=1"
                if typ == "plan"
                else "SELECT id FROM providers WHERE id=?"
            )
            if not c.execute(query, (int(target),)).fetchone():
                abort(404)
            old = c.execute(
                "SELECT id FROM subscriptions WHERE user_id=? AND target_type=? AND target_id=?",
                (uid, typ, target),
            ).fetchone()
            if old:
                c.execute("DELETE FROM subscriptions WHERE id=?", (old["id"],))
                flash("已取消订阅")
            else:
                if (
                    c.execute(
                        "SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (uid,)
                    ).fetchone()[0]
                    >= 200
                ):
                    abort(400, description="每个账户最多200条订阅")
                c.execute(
                    "INSERT INTO subscriptions(user_id,target_type,target_id,event_types,status_summary,enabled,created_at) VALUES(?,?,?,?,1,1,?)",
                    (uid, typ, target, "restock,sellout,change", w.now()),
                )
                flash("订阅已开启")
        return redirect(url_for("multiuser_subscriptions"))

    @app.route(
        "/subscriptions/subscribe", methods=["POST"], endpoint="multiuser_subscribe"
    )
    @require_user
    def subscribe():
        typ, raw = request.form.get("target_type", "").lower(), request.form.get(
            "target_id", ""
        )
        if typ not in ("plan", "provider") or not raw.isdigit() or len(raw) > 12:
            abort(400)
        with w.db() as c:
            q = (
                "SELECT id FROM sites WHERE id=? AND public=1"
                if typ == "plan"
                else "SELECT id FROM providers WHERE id=?"
            )
            row = c.execute(q, (int(raw),)).fetchone()
            if not row:
                abort(404)
            uid = current_user()["id"]
            existing = c.execute(
                "SELECT id FROM subscriptions WHERE user_id=? AND target_type=? AND target_id=?",
                (uid, typ, str(int(raw))),
            ).fetchone()
            if (
                not existing
                and c.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (uid,)
                ).fetchone()[0]
                >= 200
            ):
                abort(400, description="每个账户最多200条订阅")
            c.execute(
                "INSERT INTO subscriptions(user_id,target_type,target_id,event_types,status_summary,enabled,created_at) VALUES(?,?,?,?,1,1,?) ON CONFLICT(user_id,target_type,target_id) DO UPDATE SET enabled=1",
                (
                    current_user()["id"],
                    typ,
                    str(int(raw)),
                    "restock,sellout,change",
                    w.now(),
                ),
            )
        return redirect(url_for("multiuser_subscriptions"))

    @app.route("/account/notifications", endpoint="multiuser_notifications")
    @require_user
    def notifications():
        with w.db() as c:
            rows = c.execute(
                "SELECT * FROM user_notifications WHERE user_id=? ORDER BY id DESC LIMIT 200",
                (current_user()["id"],),
            ).fetchall()
        return render(
            "<div class=card><h2>发送记录</h2>{% for n in rows %}<p>{{n.created_at}} · {{n.channel}} · {{n.status}} · {{n.detail}}</p>{% else %}<p>暂无记录</p>{% endfor %}</div>",
            rows=rows,
        )

    _register_cli(app)
    return app


def migrate():
    w = register._w
    with w.db() as c:
        c.executescript(
            "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,is_admin INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,token_hash TEXT NOT NULL UNIQUE,user_id INTEGER NOT NULL,created_at TEXT NOT NULL,last_seen TEXT NOT NULL,expires_at TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);CREATE TABLE IF NOT EXISTS rate_limits(bucket TEXT,subject TEXT,key TEXT,attempts INTEGER DEFAULT 0,first_at REAL,last_at REAL,PRIMARY KEY(bucket,subject,key));CREATE TABLE IF NOT EXISTS telegram_prefs(user_id INTEGER PRIMARY KEY,chat_id TEXT NOT NULL,verified INTEGER DEFAULT 0,verify_hash TEXT,verify_expires TEXT,verify_attempts INTEGER DEFAULT 0,restock INTEGER DEFAULT 1,sellout INTEGER DEFAULT 1,change INTEGER DEFAULT 1,status_summary INTEGER DEFAULT 1,updated_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,target_type TEXT,target_id TEXT,event_types TEXT DEFAULT 'restock,sellout,change',status_summary INTEGER DEFAULT 1,enabled INTEGER DEFAULT 1,created_at TEXT,UNIQUE(user_id,target_type,target_id));CREATE TABLE IF NOT EXISTS user_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,event_id INTEGER,channel TEXT,status TEXT,detail TEXT DEFAULT '',created_at TEXT);"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS user_event_claims(user_id INTEGER NOT NULL,event_id INTEGER NOT NULL,PRIMARY KEY(user_id,event_id))"
        )
        cols = {x[1] for x in c.execute("PRAGMA table_info(telegram_prefs)")}
        for n, t in {
            "verify_attempts": "INTEGER DEFAULT 0",
            "restock": "INTEGER DEFAULT 1",
            "sellout": "INTEGER DEFAULT 1",
            "change": "INTEGER DEFAULT 1",
            "status_summary": "INTEGER DEFAULT 1",
        }.items():
            if n not in cols:
                c.execute(f"ALTER TABLE telegram_prefs ADD COLUMN {n} {t}")


def current_user():
    w = register._w
    t = session.get("session_token")
    if not isinstance(t, str) or len(t) < 32:
        return None
    with w.db() as c:
        r = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>? AND u.active=1",
            (_hash(t), w.now()),
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["role"] = "admin" if d.get("is_admin") else "user"
    return d


def require_user(fn):
    @wraps(fn)
    def x(*a, **k):
        return (
            fn(*a, **k)
            if current_user()
            else redirect(url_for("multiuser_login", next=request.path))
        )

    return x


def require_admin(fn):
    @wraps(fn)
    def x(*a, **k):
        if not (current_user() or {}).get("is_admin"):
            abort(403)
        return fn(*a, **k)

    return x


def csrf_token():
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _matching(sub, site):
    return (
        sub["target_type"] == "plan" and str(sub["target_id"]) == str(site["id"])
    ) or (
        sub["target_type"] == "provider"
        and str(sub["target_id"]) == str(site.get("provider_id"))
    )


def _public_link(site):
    with register._w.db() as c:
        provider = c.execute(
            "SELECT push_url FROM providers WHERE id=?", (site.get("provider_id"),)
        ).fetchone()
    return (
        provider["push_url"]
        if provider and provider["push_url"]
        else site.get("url", "")
    )


def deliver_event(event_id, site, event_type, summary, details):
    w = register._w
    if event_type not in EVENTS:
        return
    # Re-read visibility so stale input cannot disclose a recently unpublished plan.
    with w.db() as c:
        row = c.execute(
            "SELECT * FROM sites WHERE id=? AND public=1 AND enabled=1", (site["id"],)
        ).fetchone()
        if not row:
            return
        site = dict(row)
        subs = c.execute(
            "SELECT s.*,t.chat_id FROM subscriptions s JOIN users u ON u.id=s.user_id AND u.active=1 JOIN telegram_prefs t ON t.user_id=s.user_id AND t.verified=1 WHERE s.enabled=1 AND t."
            + event_type
            + "=1"
        ).fetchall()
    seen = set()
    labels = {"restock": "套餐补货", "sellout": "套餐售罄", "change": "内容变化"}
    for sub in subs:
        if (
            sub["user_id"] in seen
            or not _matching(sub, site)
            or event_type not in sub["event_types"].split(",")
        ):
            continue
        seen.add(sub["user_id"])
        message = f"[VPS Watchtower] {labels[event_type]}\n{site['name']}\n{summary}\n{_public_link(site)}"
        _deliver_one(event_id, sub, message)


def deliver_status(checked_ids):
    if not checked_ids:
        return
    w = register._w
    with w.db() as c:
        subs = c.execute(
            "SELECT s.*,t.chat_id FROM subscriptions s JOIN users u ON u.id=s.user_id AND u.active=1 JOIN telegram_prefs t ON t.user_id=s.user_id AND t.verified=1 AND t.status_summary=1 WHERE s.enabled=1 AND s.status_summary=1"
        ).fetchall()
        sites = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM sites WHERE public=1 AND enabled=1 ORDER BY id"
            )
        ]
    users = {}
    for sub in subs:
        entry = users.setdefault(sub["user_id"], {"sub": sub, "sites": {}})
        for site in sites:
            if _matching(sub, site):
                entry["sites"][site["id"]] = site
    for entry in users.values():
        if not entry["sites"]:
            continue
        header = "[VPS Watchtower] 我的订阅状态汇总"
        parts = []
        message = header
        for site in entry["sites"].values():
            freshness = (
                ("本轮已检查：" + str(checked_ids[site["id"]]))
                if site["id"] in checked_ids
                else "最近状态（本轮未检查）"
            )
            block = f"{site['name'][:160]}\n{_public_link(site)[:2048]}\n状态：{site['last_status']} · {freshness}\n最近检查：{site['last_checked'] or '尚未检查'}"
            if len(message) + len(block) + 2 > 3500:
                parts.append(message)
                message = header
            message += "\n\n" + block
        parts.append(message)
        _deliver_one(None, entry["sub"], parts)


def _deliver_one(event_id, sub, messages):
    w = register._w
    # Claim before network I/O; a crashed/ambiguous send is never automatically retried.
    with w.db() as c:
        if event_id is not None:
            claim = c.execute(
                "INSERT OR IGNORE INTO user_event_claims(user_id,event_id) VALUES(?,?)",
                (sub["user_id"], event_id),
            )
            if not claim.rowcount:
                return
        audit = c.execute(
            "INSERT INTO user_notifications(user_id,event_id,channel,status,detail,created_at) VALUES(?,?,?,?,?,?)",
            (sub["user_id"], event_id, "telegram", "pending", "", w.now()),
        ).lastrowid
    if isinstance(messages, str):
        messages = [messages]
    sent = 0
    try:
        for index, message in enumerate(messages, 1):
            _send_shared(
                message
                + (f"\n\n分段：{index}/{len(messages)}" if len(messages) > 1 else ""),
                sub["chat_id"],
            )
            sent += 1
        status, detail = "sent", f"{sent}/{len(messages)} parts sent"
    except Exception:
        status, detail = (
            "failed",
            f"Telegram delivery failed; {sent}/{len(messages)} parts sent",
        )
    with w.db() as c:
        c.execute(
            "UPDATE user_notifications SET status=?,detail=? WHERE id=?",
            (status, detail, audit),
        )


def _send_shared(msg, chat):
    import requests

    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("Telegram is not configured")
    r = requests.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        json={"chat_id": chat, "text": msg[:4000]},
        timeout=15,
        allow_redirects=False,
    )
    if r.status_code >= 400 or r.json().get("ok") is not True:
        raise RuntimeError("Telegram send failed")


def _audit(e, ch, st, de, u):
    with register._w.db() as c:
        c.execute(
            "INSERT INTO user_notifications(user_id,event_id,channel,status,detail,created_at) VALUES(?,?,?,?,?,?)",
            (u, e, ch, st, de, register._w.now()),
        )


def _hash(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


def _code_hash(code):
    return hmac.new(
        register._w.app.secret_key.encode(), str(code).encode(), hashlib.sha256
    ).hexdigest()


def _pw(s):
    return generate_password_hash(s, method="scrypt")


def _future(n):
    return datetime.fromtimestamp(time.time() + n, timezone.utc).isoformat(
        timespec="seconds"
    )


def _valid(s):
    return MIN_PASSWORD <= len(s) <= MAX_PASSWORD


def _username(s):
    s = (s or "").strip().lower()
    return s if re.fullmatch(r"[a-z0-9_.-]{3,64}", s) else ""


def _safe_next(v):
    if not v:
        return None
    if "\\" in v or any(ord(c) < 32 for c in v):
        return None
    p = urlsplit(v)
    return (
        v
        if not p.scheme
        and not p.netloc
        and v.startswith("/")
        and not v.startswith("//")
        else None
    )


def _login(uid, clear=False):
    w = register._w
    t = secrets.token_urlsafe(32)
    with w.db() as c:
        c.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,last_seen,expires_at,revoked) VALUES(?,?,?,?,?,0)",
            (_hash(t), uid, w.now(), w.now(), _future(2592000)),
        )
    if clear:
        session.clear()
    session["session_token"] = t
    session["csrf_token"] = secrets.token_urlsafe(32)


def _revoke():
    if session.get("session_token"):
        with register._w.db() as c:
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE token_hash=?",
                (_hash(session["session_token"]),),
            )


def _rate_policy(bucket):
    return {
        "login": (8, 900),
        "login_ip": (60, 900),
        "login_account": (20, 900),
        "register": (5, 3600),
        "password": (8, 900),
        "telegram_send": (1, 60),
        "telegram_test": (3, 60),
    }.get(bucket, (5, 60))


def _limited(bucket, subject, key):
    limit, window = _rate_policy(bucket)
    with register._w.db() as c:
        row = c.execute(
            "SELECT attempts,first_at FROM rate_limits WHERE bucket=? AND subject=? AND key=?",
            (bucket, subject, key),
        ).fetchone()
    return bool(
        row and row["attempts"] >= limit and time.time() - row["first_at"] < window
    )


def _hit(bucket, subject, key):
    t = time.time()
    _, window = _rate_policy(bucket)
    with register._w.db() as c:
        c.execute("DELETE FROM rate_limits WHERE last_at<?", (t - 86400 * 7,))
        c.execute(
            "INSERT INTO rate_limits VALUES(?,?,?,1,?,?) ON CONFLICT(bucket,subject,key) DO UPDATE SET attempts=CASE WHEN first_at<? THEN 1 ELSE attempts+1 END,first_at=CASE WHEN first_at<? THEN excluded.first_at ELSE first_at END,last_at=excluded.last_at",
            (bucket, subject, key, t, t, t - window, t - window),
        )


def _form(title, action, button, register=False, password=False):
    if password:
        fields = '<label>当前密码</label><input name="current" type="password" maxlength="128" autocomplete="current-password" required><label>新密码（10–128 字符）</label><input name="new" type="password" minlength="10" maxlength="128" autocomplete="new-password" required>'
    else:
        autocomplete = "new-password" if register else "current-password"
        fields = (
            '<label>用户名（3–64 位字母、数字、点、下划线或连字符）</label><input name="username" maxlength="64" autocomplete="username" required><label>密码'
            + ("（10–128 字符）" if register else "")
            + '</label><input name="password" type="password" maxlength="128" autocomplete="'
            + autocomplete
            + '" required>'
        )
    return f"<div class=card><h2>{title}</h2><form method=post action='{action}'><input type=hidden name=csrf_token value='{{{{csrf_token()}}}}'>{fields}<button>{button}</button></form></div>"


def _register_cli(app):
    import click

    @app.cli.command("create-admin")
    @click.option("--username", default="admin")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username, password):
        if not _valid(password):
            raise click.ClickException("password must be 10-128 characters")
        u = _username(username)
        if not u:
            raise click.ClickException("invalid username")
        with register._w.db() as c:
            r = c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
            if r:
                c.execute(
                    "UPDATE users SET is_admin=1,active=1,password_hash=? WHERE id=?",
                    (_pw(password), r[0]),
                )
            else:
                c.execute(
                    "INSERT INTO users(username,password_hash,is_admin,active,created_at) VALUES(?,?,?,?,?)",
                    (u, _pw(password), 1, 1, register._w.now()),
                )
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE user_id=(SELECT id FROM users WHERE username=?)",
                (u,),
            )
        click.echo("admin created")

    @app.cli.command("bootstrap")
    @click.option("--username", default="admin")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def bootstrap(username, password):
        create_admin.callback(username, password)

    @app.cli.command("reset-password")
    @click.argument("username")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def reset_password(username, password):
        if not _valid(password):
            raise click.ClickException("password must be 10-128 characters")
        with register._w.db() as c:
            r = c.execute(
                "SELECT id FROM users WHERE username=?", (_username(username),)
            ).fetchone()
            if not r:
                raise click.ClickException("user not found")
            c.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (_pw(password), r[0])
            )
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (r[0],))
        click.echo("password reset; sessions revoked")

    @app.cli.command("disable-user")
    @click.argument("username")
    def disable_user(username):
        with register._w.db() as c:
            r = c.execute(
                "SELECT id FROM users WHERE username=?", (_username(username),)
            ).fetchone()
            if not r:
                raise click.ClickException("user not found")
            c.execute("UPDATE users SET active=0 WHERE id=?", (r[0],))
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (r[0],))
        click.echo("user disabled")
