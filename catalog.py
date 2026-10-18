"""公开 VPS 套餐目录；仅负责目录与管理员元数据，不改变监控路由。"""

import math
import sqlite3
from functools import wraps
from flask import request, redirect, url_for, flash, render_template


def register(w, accounts):
    app, db, now, page = w.app, w.db, w.now, w.page
    safe_url = getattr(w, "safe_url", None)
    if safe_url is None:
        from secure_fetch import safe_url

    def migrate():
        with db() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS providers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, website TEXT DEFAULT '', description TEXT DEFAULT '', push_url TEXT DEFAULT '', created_at TEXT NOT NULL)"
            )
            pcols = {r[1] for r in c.execute("PRAGMA table_info(providers)")}
            if "push_url" not in pcols:
                c.execute("ALTER TABLE providers ADD COLUMN push_url TEXT DEFAULT ''")
            cols = {r[1] for r in c.execute("PRAGMA table_info(sites)")}
            additions = {
                "provider_id": "INTEGER",
                "location": "TEXT DEFAULT ''",
                "cpu": "TEXT DEFAULT ''",
                "ram_mb": "INTEGER",
                "disk_gb": "REAL",
                "bandwidth_gb": "REAL",
                "price": "REAL",
                "currency": "TEXT DEFAULT ''",
                "billing_period": "TEXT DEFAULT ''",
                "stock_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "soldout_keywords": "TEXT NOT NULL DEFAULT 'out of stock,sold out,缺货,售罄'",
                "public": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, typ in additions.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE sites ADD COLUMN {name} {typ}")
            evcols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
            if "kind" not in evcols:
                c.execute(
                    "ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'change'"
                )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_sites_catalog ON sites(public,provider_id,location,price)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_public ON events(kind,created_at)"
            )

    migrate()

    def user_required(fn):
        guard = getattr(accounts, "require_user", None)
        return guard(fn) if guard else fn

    def admin_required(fn):
        guard = getattr(accounts, "require_admin", None)
        return guard(fn) if guard else user_required(fn)

    def csrf():
        token = getattr(accounts, "csrf_token", lambda: "")()
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if token and supplied != token:
            raise ValueError("CSRF 校验失败")

    def val_text(key, maxlen=500, default=""):
        return (request.form.get(key, default) or "").strip()[:maxlen]

    def positive_num(key, integer=False):
        raw = (request.form.get(key) or "").strip()
        if not raw:
            return None
        try:
            x = int(raw) if integer else float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{key} 必须是非负数字")
        if not math.isfinite(x) or x < 0 or x > 10**9:
            raise ValueError(f"{key} 必须是非负有限数字")
        return x

    def website(value):
        value = (value or "").strip()
        return safe_url(value) if value else ""

    def public_where(args):
        clauses, vals = ["s.public=1"], []
        q = (args.get("q") or "").strip()[:100]
        if q:
            clauses.append(
                "(s.name LIKE ? OR s.location LIKE ? OR p.name LIKE ? OR s.cpu LIKE ?)"
            )
            vals += [f"%{q}%"] * 4
        for field, key in (
            ("p.id", "provider"),
            ("s.location", "location"),
            ("s.stock_status", "stock"),
            ("s.currency", "currency"),
        ):
            v = (args.get(key) or "").strip()
            if v and ((field == "p.id" and v.isdigit()) or field != "p.id"):
                clauses.append(field + "=?")
                vals.append(int(v) if field == "p.id" else v[:100])
        # Price is meaningful only with BOTH currency and billing period filters.
        currency = (args.get("currency") or "").strip()
        period = (args.get("billing_period") or "").strip()
        if currency and not any("s.currency=?" in x for x in clauses):
            clauses.append("s.currency=?")
            vals.append(currency[:12])
        if period:
            clauses.append("s.billing_period=?")
            vals.append(period[:40])
        if (args.get("max_price") or "").strip() and currency and period:
            try:
                x = float(args.get("max_price"))
            except ValueError:
                x = None
            if x is not None and math.isfinite(x):
                clauses.append("s.price<=?")
                vals.append(x)
        for field, key in (
            ("s.ram_mb", "min_ram"),
            ("s.disk_gb", "min_disk"),
            ("s.bandwidth_gb", "min_bandwidth"),
        ):
            raw = (args.get(key) or "").strip()
            if raw:
                try:
                    x = float(raw)
                except ValueError:
                    continue
                if math.isfinite(x):
                    clauses.append(field + ">=?")
                    vals.append(x)
        return " AND ".join(clauses), vals

    def render(name, **context):
        context.setdefault("csrf_token", getattr(accounts, "csrf_token", lambda: "")())
        # page(_rendered=True) prevents user-controlled {{...}} from a first render becoming code.
        return page(render_template(name, **context), _rendered=True)

    @app.route("/catalog")
    def catalog():
        where, vals = public_where(request.args)
        sort = request.args.get("sort", "newest")
        allowed = {
            "price_asc": "s.price ASC",
            "price_desc": "s.price DESC",
            "name": "s.name COLLATE NOCASE ASC",
            "newest": "s.id DESC",
            "stock": "s.stock_status ASC",
        }
        # Never rank unlike currencies or billing periods; incomplete price filters fall back to newest.
        if sort in ("price_asc", "price_desc") and not (
            (request.args.get("currency") or "").strip()
            and (request.args.get("billing_period") or "").strip()
        ):
            sort = "newest"
        order = allowed.get(sort, allowed["newest"])
        try:
            per = min(60, max(1, int(request.args.get("per_page", 12))))
        except ValueError:
            per = 12
        try:
            current = max(1, int(request.args.get("page", 1)))
        except ValueError:
            current = 1
        with db() as c:
            providers = [
                dict(x)
                for x in c.execute(
                    "SELECT id,name FROM providers ORDER BY name COLLATE NOCASE"
                )
            ]
            total = c.execute(
                f"SELECT COUNT(*) FROM sites s LEFT JOIN providers p ON p.id=s.provider_id WHERE {where}",
                vals,
            ).fetchone()[0]
            pages = max(1, math.ceil(total / per))
            current = min(current, pages)
            rows = [
                dict(x)
                for x in c.execute(
                    f"SELECT s.id,s.name,s.location,s.cpu,s.ram_mb,s.disk_gb,s.bandwidth_gb,s.price,s.currency,s.billing_period,s.stock_status,p.id provider_id,p.name provider_name FROM sites s LEFT JOIN providers p ON p.id=s.provider_id WHERE {where} ORDER BY {order},s.id DESC LIMIT ? OFFSET ?",
                    vals + [per, (current - 1) * per],
                )
            ]
        allowed_query = {
            "q",
            "provider",
            "location",
            "stock",
            "currency",
            "billing_period",
            "max_price",
            "min_ram",
            "min_disk",
            "min_bandwidth",
            "sort",
            "per_page",
        }
        params = {
            key: value for key, value in request.args.items() if key in allowed_query
        }

        def page_url(number):
            return url_for("catalog", **dict(params, page=number))

        return render(
            "catalog.html",
            plans=rows,
            providers=providers,
            total=total,
            page_no=current,
            pages=pages,
            query=request.args,
            previous_url=page_url(current - 1) if current > 1 else None,
            next_url=page_url(current + 1) if current < pages else None,
        )

    @app.route("/providers")
    def providers_public():
        with db() as c:
            providers = [
                dict(x)
                for x in c.execute(
                    "SELECT p.id,p.name,p.website,p.description,p.push_url,COUNT(s.id) plan_count FROM providers p LEFT JOIN sites s ON s.provider_id=p.id AND s.public=1 GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
                )
            ]
        return render("providers.html", providers=providers)

    @app.route("/providers/<int:provider_id>")
    def provider_public(provider_id):
        with db() as c:
            provider = c.execute(
                "SELECT id,name,website,description,push_url FROM providers WHERE id=?",
                (provider_id,),
            ).fetchone()
        if not provider:
            return "供应商不存在", 404
        args = request.args.copy()
        args["provider"] = str(provider_id)
        where, vals = public_where(args)
        with db() as c:
            plans = [
                dict(x)
                for x in c.execute(
                    f"SELECT s.id,s.name,s.location,s.cpu,s.ram_mb,s.disk_gb,s.bandwidth_gb,s.price,s.currency,s.billing_period,s.stock_status,p.name provider_name FROM sites s LEFT JOIN providers p ON p.id=s.provider_id WHERE {where} ORDER BY s.id DESC",
                    vals,
                )
            ]
        return render("provider.html", provider=dict(provider), plans=plans)

    @app.route("/plans/<int:site_id>")
    def plan_public(site_id):
        with db() as c:
            plan = c.execute(
                "SELECT s.id,s.name,s.url,s.location,s.cpu,s.ram_mb,s.disk_gb,s.bandwidth_gb,s.price,s.currency,s.billing_period,s.stock_status,s.public,p.name provider_name,p.website provider_website FROM sites s LEFT JOIN providers p ON p.id=s.provider_id WHERE s.id=? AND s.public=1",
                (site_id,),
            ).fetchone()
            if not plan:
                return "套餐不存在", 404
            events = c.execute(
                "SELECT kind,summary,created_at FROM events WHERE site_id=? AND event_type!='全站状态汇总' AND kind IN ('restock','sellout','change') ORDER BY id DESC LIMIT 8",
                (site_id,),
            ).fetchall()
        return render("plan.html", plan=dict(plan), events=[dict(x) for x in events])

    @app.route("/activity")
    def activity():
        try:
            limit = min(100, max(1, int(request.args.get("limit", 40))))
        except ValueError:
            limit = 40
        with db() as c:
            events = [
                dict(x)
                for x in c.execute(
                    "SELECT e.kind,e.summary,e.created_at,s.id site_id,s.name site_name,p.name provider_name FROM events e JOIN sites s ON s.id=e.site_id LEFT JOIN providers p ON p.id=s.provider_id WHERE s.public=1 AND e.event_type!='全站状态汇总' AND e.kind IN ('restock','sellout','change') ORDER BY e.id DESC LIMIT ?",
                    (limit,),
                )
            ]
        return render("activity.html", events=events)

    @app.route("/admin/providers", methods=["GET", "POST"])
    @admin_required
    def admin_providers():
        if request.method == "POST":
            try:
                csrf()
                name = val_text("name", 160)
                if not name:
                    raise ValueError("供应商名称不能为空")
                with db() as c:
                    c.execute(
                        "INSERT INTO providers(name,website,description,push_url,created_at) VALUES(?,?,?,?,?)",
                        (
                            name,
                            website(val_text("website", 500)),
                            val_text("description", 3000),
                            website(val_text("push_url", 500)),
                            now(),
                        ),
                    )
                flash("供应商已创建")
            except Exception as e:
                flash(str(e))
            return redirect(url_for("admin_providers"))
        with db() as c:
            providers = [
                dict(x) for x in c.execute("SELECT * FROM providers ORDER BY id DESC")
            ]
        return render("admin_providers.html", providers=providers)

    @app.route("/admin/providers/<int:provider_id>/edit", methods=["POST"])
    @admin_required
    def admin_provider_edit(provider_id):
        try:
            csrf()
            name = val_text("name", 160)
            if not name:
                raise ValueError("供应商名称不能为空")
            with db() as c:
                cur = c.execute(
                    "UPDATE providers SET name=?,website=?,description=?,push_url=? WHERE id=?",
                    (
                        name,
                        website(val_text("website", 500)),
                        val_text("description", 3000),
                        website(val_text("push_url", 500)),
                        provider_id,
                    ),
                )
            if not cur.rowcount:
                return "供应商不存在", 404
            flash("供应商已更新")
        except Exception as e:
            flash(str(e))
        return redirect(url_for("admin_providers"))

    @app.route("/admin/providers/<int:provider_id>/delete", methods=["POST"])
    @admin_required
    def admin_provider_delete(provider_id):
        try:
            csrf()
            with db() as c:
                c.execute(
                    "UPDATE sites SET provider_id=NULL WHERE provider_id=?",
                    (provider_id,),
                )
                try:
                    c.execute(
                        "DELETE FROM subscriptions WHERE target_type='provider' AND target_id=?",
                        (str(provider_id),),
                    )
                except sqlite3.OperationalError:
                    pass
                c.execute("DELETE FROM providers WHERE id=?", (provider_id,))
            flash("供应商已删除，关联套餐保留")
        except Exception as e:
            flash(str(e))
        return redirect(url_for("admin_providers"))

    @app.route("/admin/plans/<int:site_id>", methods=["GET", "POST"])
    @admin_required
    def admin_plan(site_id):
        if request.method == "POST":
            try:
                csrf()
                raw = val_text("provider_id", 20)
                provider = int(raw) if raw.isdigit() else None
                status = val_text("stock_status", 30) or "unknown"
                if status not in {"in_stock", "out_of_stock", "unknown"}:
                    raise ValueError("库存状态无效")
                vals = (
                    provider,
                    val_text("location", 160),
                    val_text("cpu", 160),
                    positive_num("ram_mb", True),
                    positive_num("disk_gb"),
                    positive_num("bandwidth_gb"),
                    positive_num("price"),
                    val_text("currency", 12).upper(),
                    val_text("billing_period", 40),
                    status,
                    val_text("soldout_keywords", 300),
                    1 if request.form.get("public") in ("1", "on", "true") else 0,
                    site_id,
                )
                with db() as c:
                    if (
                        provider is not None
                        and not c.execute(
                            "SELECT 1 FROM providers WHERE id=?", (provider,)
                        ).fetchone()
                    ):
                        raise ValueError("供应商不存在")
                    cur = c.execute(
                        "UPDATE sites SET provider_id=?,location=?,cpu=?,ram_mb=?,disk_gb=?,bandwidth_gb=?,price=?,currency=?,billing_period=?,stock_status=?,soldout_keywords=?,public=? WHERE id=?",
                        vals,
                    )
                if not cur.rowcount:
                    return "套餐不存在", 404
                flash("套餐元数据已保存")
            except Exception as e:
                flash(str(e))
            return redirect(url_for("admin_plan", site_id=site_id))
        with db() as c:
            plan = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
            if not plan:
                return "套餐不存在", 404
            providers = [
                dict(x)
                for x in c.execute("SELECT id,name FROM providers ORDER BY name")
            ]
        return render("admin_plan.html", plan=dict(plan), providers=providers)

    return migrate
