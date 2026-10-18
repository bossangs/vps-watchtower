import os, re, csv, io, json, time, sqlite3, threading, smtplib, logging
from email.message import EmailMessage
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, session, flash, Response, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("watchtower")
APP_SECRET = os.getenv("APP_SECRET", "dev-only-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")
DB_PATH = os.getenv("DATABASE_PATH", "/data/watchtower.db")
DEFAULT_INTERVAL = max(60, int(os.getenv("DEFAULT_INTERVAL", "300")))
TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "20")))
MAX_BYTES = max(1024, int(os.getenv("MAX_RESPONSE_BYTES", "2097152")))
USER_AGENT = os.getenv("USER_AGENT", "VPS-Watchtower/1.0")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

db_lock = threading.Lock()
check_lock = threading.Lock()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
          interval_seconds INTEGER NOT NULL DEFAULT 300, selector TEXT DEFAULT '',
          keywords TEXT DEFAULT '', price_threshold REAL, stock_keywords TEXT DEFAULT 'in stock,available,有货',
          channels TEXT DEFAULT 'telegram,email,discord,webhook', enabled INTEGER NOT NULL DEFAULT 1,
          last_checked TEXT, last_status TEXT DEFAULT 'Never checked', last_error TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
          content TEXT NOT NULL, checked_at TEXT NOT NULL,
          FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_site ON snapshots(site_id, id DESC);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          summary TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, channel TEXT NOT NULL,
          status TEXT NOT NULL, detail TEXT DEFAULT '', created_at TEXT NOT NULL,
          FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        """)
init_db()

def safe_url(raw):
    p = urlparse((raw or "").strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("URL must start with http:// or https://")
    # Prevent local/private network targets to reduce SSRF risk.
    host = (p.hostname or "").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise ValueError("Local hostnames are not allowed")
    return p.geturl()

def clean_html(html, selector=""):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    if selector:
        selected = soup.select(selector)
        if not selected:
            raise ValueError(f"CSS selector matched no elements: {selector}")
        text = "\n".join(el.get_text(" ", strip=True) for el in selected)
    else:
        text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def fetch_site(site):
    url = safe_url(site["url"])
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                     allow_redirects=True, stream=True)
    r.raise_for_status()
    chunks, total = [], 0
    for chunk in r.iter_content(65536):
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f"Response exceeds MAX_RESPONSE_BYTES ({MAX_BYTES})")
        chunks.append(chunk)
    body = b"".join(chunks)
    encoding = r.encoding or "utf-8"
    html = body.decode(encoding, errors="replace")
    return clean_html(html, site["selector"] or "")

def digest(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

def parse_price(text):
    # Conservative heuristic; use selectors to narrow content to a price element.
    matches = re.findall(r'(?i)(?:USD|US\$|\$)\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)', text)
    if not matches:
        matches = re.findall(r'(?i)([0-9]+(?:\.[0-9]{1,2})?)\s*(?:USD|US\$)', text)
    vals = []
    for x in matches:
        try: vals.append(float(x.replace(",", "")))
        except ValueError: pass
    return min(vals) if vals else None

def send_telegram(message):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat: raise RuntimeError("Telegram credentials not configured")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": message[:4000], "disable_web_page_preview": True}, timeout=15)
    r.raise_for_status()

def send_email(subject, message):
    host = os.getenv("SMTP_HOST")
    to = os.getenv("SMTP_TO")
    if not host or not to: raise RuntimeError("SMTP_HOST or SMTP_TO not configured")
    msg = EmailMessage()
    msg["Subject"] = subject[:180]
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "watchtower@localhost"
    msg["To"] = to
    msg.set_content(message)
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as s:
        if os.getenv("SMTP_STARTTLS", "true").lower() == "true": s.starttls()
        if os.getenv("SMTP_USER"): s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)

def send_discord(message):
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url: raise RuntimeError("DISCORD_WEBHOOK_URL not configured")
    r = requests.post(url, json={"content": message[:1900]}, timeout=15)
    r.raise_for_status()

def send_webhook(message, event):
    url = os.getenv("GENERIC_WEBHOOK_URL")
    if not url: raise RuntimeError("GENERIC_WEBHOOK_URL not configured")
    r = requests.post(url, json={"text": message, "event": event}, timeout=15)
    r.raise_for_status()

SENDERS = {"telegram": send_telegram, "email": send_email, "discord": send_discord}

def notify(event_id, site, event_type, summary, details):
    message = f"[VPS Watchtower] {event_type}\n{site['name']}\n{site['url']}\n{summary}\n\n{details[:2500]}"
    channels = [x.strip().lower() for x in (site["channels"] or "").split(",") if x.strip()]
    for channel in channels:
        try:
            if channel in SENDERS: SENDERS[channel](message)
            elif channel == "webhook": send_webhook(message, {"id": event_id, "type": event_type, "site": site["url"]})
            else: raise RuntimeError(f"Unknown channel: {channel}")
            status, detail = "sent", ""
        except Exception as e:
            status, detail = "failed", str(e)[:500]
            log.warning("Notification %s failed: %s", channel, detail)
        with db() as c:
            c.execute("INSERT INTO notifications(event_id,channel,status,detail,created_at) VALUES(?,?,?,?,?)",
                      (event_id, channel, status, detail, now()))

def check_one(site_id):
    with db() as c:
        row = c.execute("SELECT * FROM sites WHERE id=? AND enabled=1", (site_id,)).fetchone()
        if not row: return
        site = dict(row)
    try:
        content = fetch_site(site)
        h = digest(content)
        with db() as c:
            previous = c.execute("SELECT content_hash,content FROM snapshots WHERE site_id=? ORDER BY id DESC LIMIT 1", (site_id,)).fetchone()
            c.execute("INSERT INTO snapshots(site_id,content_hash,content,checked_at) VALUES(?,?,?,?)",
                      (site_id, h, content[:200000], now()))
            c.execute("UPDATE sites SET last_checked=?,last_status=?,last_error='' WHERE id=?",
                      (now(), "OK", site_id))
            if not previous:
                log.info("Baseline created for site %s", site_id)
                return
            old = previous["content"]
        changed = h != previous["content_hash"]
        keywords = [k.strip() for k in (site["keywords"] or "").split(",") if k.strip()]
        keyword_hits = [k for k in keywords if k.casefold() in content.casefold() and k.casefold() not in old.casefold()]
        threshold = site["price_threshold"]
        price = parse_price(content) if threshold is not None else None
        price_hit = threshold is not None and price is not None and price < float(threshold)
        stock_words = [k.strip() for k in (site["stock_keywords"] or "").split(",") if k.strip()]
        stock_hit = any(k.casefold() in content.casefold() and k.casefold() not in old.casefold() for k in stock_words)
        if changed:
            event_type = "网页内容变化"
            summary = "监控内容与上次快照不同。"
            if keyword_hits: summary += " 新关键词：" + ", ".join(keyword_hits)
            if price_hit: summary += f" 检测到价格约 {price:g}，低于阈值 {float(threshold):g}。"
            if stock_hit: summary += " 检测到库存关键词新增。"
            # Keep event details concise to avoid sending full page dumps.
            details = f"上次内容：{old[:800]}\n当前内容：{content[:1200]}"
            with db() as c:
                cur = c.execute("INSERT INTO events(site_id,event_type,summary,details,created_at) VALUES(?,?,?,?,?)",
                                (site_id, event_type, summary, details, now()))
                eid = cur.lastrowid
            notify(eid, site, event_type, summary, details)
    except Exception as e:
        msg = str(e)[:1000]
        with db() as c:
            c.execute("UPDATE sites SET last_checked=?,last_status=?,last_error=? WHERE id=?",
                      (now(), "ERROR", msg, site_id))
        log.exception("Check failed for site %s", site_id)

def due_checks():
    if not check_lock.acquire(blocking=False): return
    try:
        with db() as c:
            rows = c.execute("""SELECT id, interval_seconds, last_checked FROM sites WHERE enabled=1""").fetchall()
        current = time.time()
        for row in rows:
            last = row["last_checked"]
            due = True
            if last:
                try:
                    last_ts = datetime.fromisoformat(last).timestamp()
                    due = current - last_ts >= max(60, int(row["interval_seconds"]))
                except Exception: pass
            if due: check_one(row["id"])
    finally:
        check_lock.release()

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"): return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

BASE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VPS Watchtower</title><style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#172033;background:#f4f6fa}body{margin:0}
header{background:#101827;color:white;padding:16px 22px;display:flex;justify-content:space-between;align-items:center}
header a{color:#dbeafe;margin-left:12px;text-decoration:none}.wrap{max-width:1200px;margin:24px auto;padding:0 14px}
.card{background:white;border:1px solid #dce2ec;border-radius:12px;padding:18px;margin-bottom:16px;overflow:auto}
h1{font-size:22px;margin:0}h2{font-size:18px;margin:0 0 12px}input,textarea,select{box-sizing:border-box;width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:7px;margin:4px 0 12px}
label{font-size:13px;color:#475569}button,.btn{display:inline-block;border:0;border-radius:7px;background:#2563eb;color:white;padding:9px 13px;text-decoration:none;cursor:pointer}
.btn.gray{background:#475569}.btn.red{background:#b91c1c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;vertical-align:top}td a{margin-right:6px}
small,.muted{color:#64748b}.flash{padding:10px;background:#fff7ed;border:1px solid #fed7aa;border-radius:7px;margin-bottom:10px}
.status-OK{color:#15803d}.status-ERROR{color:#b91c1c}.status-Never{color:#64748b}
@media(max-width:700px){th:nth-child(3),td:nth-child(3){display:none}}
</style></head><body><header><h1>VPS Watchtower</h1><nav><a href="{{ url_for('index') }}">监控站点</a><a href="{{ url_for('events') }}">变化事件</a><a href="{{ url_for('settings') }}">通知设置</a><a href="{{ url_for('logout') }}">退出</a></nav></header>
<div class="wrap">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}{{ body|safe }}</div></body></html>
"""
def page(body, **kwargs):
    return render_template_string(BASE, body=render_template_string(body, **kwargs))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("password","") == ADMIN_PASSWORD and ADMIN_PASSWORD != "change-this-password":
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("密码错误，或尚未更改默认 ADMIN_PASSWORD。")
    return render_template_string("""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 Watchtower</title><style>body{font-family:system-ui;background:#f4f6fa;max-width:380px;margin:15vh auto;padding:20px}form{background:white;padding:24px;border-radius:12px}input,button{width:100%;padding:12px;margin:8px 0;box-sizing:border-box}</style><form method="post"><h2>VPS Watchtower 登录</h2><p>请输入 ADMIN_PASSWORD</p><input type="password" name="password" required><button>登录</button>{% for m in get_flashed_messages() %}<p>{{m}}</p>{% endfor %}</form>""")
@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    with db() as c:
        sites = [dict(x) for x in c.execute("SELECT * FROM sites ORDER BY id DESC")]
        events_count = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    body = """
    <div class="grid">
      <div class="card"><small>监控站点</small><h2>{{sites|length}}</h2></div>
      <div class="card"><small>启用中</small><h2>{{sites|selectattr('enabled')|list|length}}</h2></div>
      <div class="card"><small>历史事件</small><h2>{{events_count}}</h2></div>
    </div>
    <div class="card"><h2>添加监控网站</h2>
    <form method="post" action="{{url_for('add_site')}}">
      <div class="grid"><div><label>名称</label><input name="name" required placeholder="例如：商家套餐页面"></div>
      <div><label>网址</label><input name="url" required type="url" placeholder="https://example.com/plans"></div>
      <div><label>检查间隔（秒，最少 60）</label><input name="interval_seconds" type="number" min="60" value="{{default_interval}}"></div>
      <div><label>CSS 选择器（可选）</label><input name="selector" placeholder=".pricing-table 或 #stock"></div>
      <div><label>关键词（逗号分隔）</label><input name="keywords" placeholder="补货,新套餐,东京"></div>
      <div><label>价格阈值（可选，USD 粗略识别）</label><input name="price_threshold" type="number" step="0.01" placeholder="例如 5.00"></div>
      <div><label>库存关键词</label><input name="stock_keywords" value="in stock,available,有货,库存充足"></div>
      <div><label>通知渠道（逗号分隔）</label><input name="channels" value="telegram,email,discord,webhook"></div></div>
      <button>添加网站</button>
    </form></div>
    <div class="card"><h2>监控站点</h2><p><a class="btn gray" href="{{url_for('export_csv')}}">导出 CSV</a> <a class="btn gray" href="{{url_for('import_page')}}">批量导入 CSV</a> <a class="btn gray" href="{{url_for('check_all')}}">立即检查全部</a></p>
    <table><thead><tr><th>名称 / URL</th><th>状态</th><th>间隔</th><th>规则</th><th>操作</th></tr></thead><tbody>
    {% for s in sites %}<tr><td><b>{{s.name}}</b><br><small><a href="{{s.url}}" target="_blank" rel="noreferrer">{{s.url}}</a></small></td>
    <td class="status-{{s.last_status if s.last_status in ['OK','ERROR'] else 'Never'}}">{{s.last_status}}<br><small>{{s.last_checked or '尚未检查'}}</small>{% if s.last_error %}<br><small>{{s.last_error[:120]}}</small>{% endif %}</td>
    <td>{{s.interval_seconds}} 秒</td><td><small>CSS: {{s.selector or '整页'}}<br>关键词: {{s.keywords or '无'}}<br>渠道: {{s.channels}}</small></td>
    <td><a class="btn gray" href="{{url_for('edit_site',site_id=s.id)}}">编辑</a><a class="btn gray" href="{{url_for('check_site',site_id=s.id)}}">检查</a>
    <a class="btn gray" href="{{url_for('toggle_site',site_id=s.id)}}">{{'暂停' if s.enabled else '启用'}}</a>
    <form method="post" action="{{url_for('delete_site',site_id=s.id)}}" onsubmit="return confirm('确认删除？')" style="display:inline"><button class="btn red">删除</button></form></td></tr>{% endfor %}
    </tbody></table></div>"""
    return page(body, sites=sites, events_count=events_count, default_interval=DEFAULT_INTERVAL)

@app.route("/sites/add", methods=["POST"])
@login_required
def add_site():
    try:
        url = safe_url(request.form.get("url"))
        interval = max(60, min(86400, int(request.form.get("interval_seconds") or DEFAULT_INTERVAL)))
        threshold_raw = request.form.get("price_threshold","").strip()
        threshold = float(threshold_raw) if threshold_raw else None
        with db() as c:
            c.execute("""INSERT INTO sites(name,url,interval_seconds,selector,keywords,price_threshold,stock_keywords,channels,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (request.form.get("name","").strip() or url, url, interval,
            request.form.get("selector","").strip(), request.form.get("keywords","").strip(), threshold,
            request.form.get("stock_keywords","").strip(), request.form.get("channels","telegram,email,discord,webhook").strip(), now()))
        flash("网站已添加；首次检查会建立基线快照。")
    except Exception as e: flash(f"添加失败：{e}")
    return redirect(url_for("index"))

@app.route("/sites/<int:site_id>/edit", methods=["GET","POST"])
@login_required
def edit_site(site_id):
    with db() as c: row = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not row: flash("站点不存在"); return redirect(url_for("index"))
    s = dict(row)
    if request.method == "POST":
        try:
            url = safe_url(request.form.get("url"))
            interval = max(60, min(86400, int(request.form.get("interval_seconds") or DEFAULT_INTERVAL)))
            tr = request.form.get("price_threshold","").strip()
            threshold = float(tr) if tr else None
            with db() as c:
                c.execute("""UPDATE sites SET name=?,url=?,interval_seconds=?,selector=?,keywords=?,price_threshold=?,stock_keywords=?,channels=? WHERE id=?""",
                (request.form.get("name","").strip(),url,interval,request.form.get("selector","").strip(),
                request.form.get("keywords","").strip(),threshold,request.form.get("stock_keywords","").strip(),
                request.form.get("channels","").strip(),site_id))
            flash("配置已保存"); return redirect(url_for("index"))
        except Exception as e: flash(f"保存失败：{e}")
    body = """<div class="card"><h2>编辑监控站点 #{{s.id}}</h2><form method="post">
    <label>名称</label><input name="name" required value="{{s.name}}">
    <label>网址</label><input name="url" type="url" required value="{{s.url}}">
    <label>检查间隔（秒）</label><input name="interval_seconds" type="number" min="60" value="{{s.interval_seconds}}">
    <label>CSS 选择器</label><input name="selector" value="{{s.selector}}">
    <label>关键词（逗号分隔）</label><input name="keywords" value="{{s.keywords}}">
    <label>价格阈值（USD 粗略识别）</label><input name="price_threshold" type="number" step="0.01" value="{{s.price_threshold if s.price_threshold is not none else ''}}">
    <label>库存关键词</label><input name="stock_keywords" value="{{s.stock_keywords}}">
    <label>通知渠道</label><input name="channels" value="{{s.channels}}">
    <button>保存</button> <a class="btn gray" href="{{url_for('index')}}">取消</a></form></div>"""
    return page(body, s=s)

@app.route("/sites/<int:site_id>/delete", methods=["POST"])
@login_required
def delete_site(site_id):
    with db() as c: c.execute("DELETE FROM sites WHERE id=?", (site_id,))
    flash("站点及相关历史已删除"); return redirect(url_for("index"))

@app.route("/sites/<int:site_id>/toggle")
@login_required
def toggle_site(site_id):
    with db() as c: c.execute("UPDATE sites SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (site_id,))
    return redirect(url_for("index"))

@app.route("/sites/<int:site_id>/check")
@login_required
def check_site(site_id):
    check_one(site_id); flash("已执行检查；查看状态和事件列表确认结果。")
    return redirect(url_for("index"))

@app.route("/check-all")
@login_required
def check_all():
    with db() as c: ids = [x[0] for x in c.execute("SELECT id FROM sites WHERE enabled=1")]
    for sid in ids: check_one(sid)
    flash(f"已触发 {len(ids)} 个站点检查。")
    return redirect(url_for("index"))

@app.route("/events")
@login_required
def events():
    with db() as c:
        rows = [dict(x) for x in c.execute("""SELECT e.*,s.name,s.url FROM events e JOIN sites s ON s.id=e.site_id ORDER BY e.id DESC LIMIT 300""")]
        notes = {}
        for r in rows:
            notes[r["id"]] = [dict(n) for n in c.execute("SELECT channel,status,detail,created_at FROM notifications WHERE event_id=? ORDER BY id", (r["id"],))]
    body = """<div class="card"><h2>变化事件（最近 300 条）</h2><table><tr><th>时间</th><th>站点</th><th>事件</th><th>摘要 / 内容差异</th><th>通知状态</th></tr>
    {% for e in rows %}<tr><td>{{e.created_at}}</td><td>{{e.name}}<br><small>{{e.url}}</small></td><td>{{e.event_type}}</td><td>{{e.summary}}<details><summary>查看差异</summary><pre style="white-space:pre-wrap">{{e.details}}</pre></details></td><td>{% for n in notes[e.id] %}<small>{{n.channel}}: {{n.status}} {{n.detail}}</small><br>{% endfor %}</td></tr>{% endfor %}</table></div>"""
    return page(body, rows=rows, notes=notes)

@app.route("/settings")
@login_required
def settings():
    names = ["TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASSWORD","SMTP_FROM","SMTP_TO","SMTP_STARTTLS","DISCORD_WEBHOOK_URL","GENERIC_WEBHOOK_URL"]
    body = """<div class="card"><h2>通知渠道配置</h2><p>出于安全考虑，密钥只从服务器的 <code>.env</code> 文件读取，不在此页面显示或编辑。</p><p>当前环境变量是否已配置：</p><ul>{% for n in names %}<li>{{n}}：<b>{{'已配置' if env[n] else '未配置'}}</b></li>{% endfor %}</ul><p>修改 .env 后运行 <code>docker compose up -d</code> 使配置生效。每个站点可单独选择渠道。</p></div>"""
    return page(body, names=names, env={n:bool(os.getenv(n)) for n in names})

@app.route("/import")
@login_required
def import_page():
    body = """<div class="card"><h2>批量导入 CSV</h2><p>CSV 至少包含 <code>name,url</code>，可选列：<code>interval_seconds,selector,keywords,price_threshold,stock_keywords,channels</code>。请使用 UTF-8 编码。</p><form method="post" action="{{url_for('import_csv')}}" enctype="multipart/form-data"><input type="file" name="file" accept=".csv,text/csv" required><button>导入</button></form><p><a href="{{url_for('export_csv')}}">下载当前站点 CSV</a></p></div>"""
    return page(body)

@app.route("/import.csv", methods=["POST"])
@login_required
def import_csv():
    f = request.files.get("file")
    if not f: flash("请选择 CSV 文件"); return redirect(url_for("import_page"))
    try:
        content = f.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        with db() as c:
            for row in reader:
                url = safe_url(row.get("url",""))
                name = (row.get("name") or url).strip()
                interval = max(60, min(86400, int(row.get("interval_seconds") or DEFAULT_INTERVAL)))
                tr = (row.get("price_threshold") or "").strip()
                threshold = float(tr) if tr else None
                c.execute("""INSERT INTO sites(name,url,interval_seconds,selector,keywords,price_threshold,stock_keywords,channels,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (name,url,interval,row.get("selector",""),row.get("keywords",""),threshold,
                row.get("stock_keywords","in stock,available,有货"),row.get("channels","telegram,email,discord,webhook"),now()))
                count += 1
        flash(f"成功导入 {count} 个网站")
    except Exception as e: flash(f"导入失败：{e}")
    return redirect(url_for("index"))

@app.route("/export.csv")
@login_required
def export_csv():
    with db() as c: rows = [dict(x) for x in c.execute("SELECT name,url,interval_seconds,selector,keywords,price_threshold,stock_keywords,channels,enabled FROM sites ORDER BY id")]
    out = io.StringIO()
    fields = ["name","url","interval_seconds","selector","keywords","price_threshold","stock_keywords","channels","enabled"]
    w = csv.DictWriter(out, fieldnames=fields); w.writeheader(); w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":"attachment; filename=watchtower-sites.csv"})

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(due_checks, "interval", seconds=30, id="site-poller", max_instances=1, coalesce=True)
scheduler.start()

if __name__ == "__main__":
    # Avoid Flask debug reloader because it would start duplicate schedulers.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")), debug=False, threaded=True)
