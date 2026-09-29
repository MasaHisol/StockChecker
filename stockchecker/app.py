from datetime import date

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import db, service
from .config import Settings
from .providers import DemoProvider, HttpJsonProvider, parse_price_csv


def build_providers(settings, demo=False):
    ps = []
    if settings.price_feed_url:
        ps.append(HttpJsonProvider(settings.price_feed_url))
    if demo:
        ps.append(DemoProvider())
    return ps


def _int(v):
    return int(v) if v not in (None, "") else None


def _float(v):
    return float(v.replace(",", "")) if v not in (None, "") else None


def create_app(settings=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "stockchecker-local"
    app.config["SC"] = settings

    with db.connect(settings.database) as conn:
        db.init_db(conn)

    def conn():
        if "conn" not in g:
            g.conn = db.connect(settings.database)
        return g.conn

    @app.teardown_appcontext
    def _close(_):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    def get_or_404(table, id_):
        row = conn().execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone()
        if row is None:
            abort(404)
        return row

    @app.context_processor
    def _ctx():
        return {"settings": settings}

    # ---- ダッシュボード --------------------------------------------------
    @app.route("/")
    def index():
        c = conn()
        materials = c.execute(
            "SELECT m.*, s.name AS owner_name, sp.name AS supplier_name FROM materials m "
            "LEFT JOIN staff s ON s.id=m.owner_id "
            "LEFT JOIN suppliers sp ON sp.id=m.preferred_supplier_id "
            "WHERE m.active=1 ORDER BY m.required_date IS NULL, m.required_date").fetchall()
        rows = []
        for m in materials:
            latest = db.latest_observation(c, m["id"])
            findings = service.evaluate_material(c, m, settings)
            rows.append({"m": m, "latest": latest, "findings": findings,
                         "needs_quote": any(f.needs_quote for f in findings)})
        alerts = c.execute(
            "SELECT a.*, m.name, m.part_number FROM alerts a JOIN materials m ON m.id=a.material_id "
            "WHERE a.status='open' ORDER BY a.created_at DESC LIMIT 50").fetchall()
        drafts = c.execute("SELECT COUNT(*) FROM emails WHERE status='draft'").fetchone()[0]
        return render_template("index.html", rows=rows, alerts=alerts, drafts=drafts)

    @app.post("/run-checks")
    def run_checks():
        demo = request.form.get("demo") == "1"
        s = service.run_checks(conn(), settings, build_providers(settings, demo))
        flash(f"チェック完了: 部材 {s['materials']} 件 / 価格取得 {s['observations']} 件 / "
              f"新規アラート {s['new_alerts']} 件 / 見積依頼下書き {s['rfq_drafts']} 件 "
              f"(自動送信 {s['rfq_sent']} 件) / 担当者通知 {s['notifications']} 件")
        return redirect(url_for("index"))

    # ---- 部材 ------------------------------------------------------------
    def material_form_values():
        f = request.form
        return (f["part_number"].strip(), f["name"].strip(), f.get("maker") or None,
                f.get("spec") or None, f.get("unit") or "個", _int(f.get("quantity")) or 1,
                f.get("required_date") or None, _float(f.get("budget_unit_price")),
                1 if f.get("custom_item") else 0, _int(f.get("owner_id")),
                _int(f.get("preferred_supplier_id")), f.get("notes") or None)

    def masters():
        c = conn()
        return {"staff": c.execute("SELECT * FROM staff ORDER BY name").fetchall(),
                "suppliers": c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()}

    @app.route("/materials/new", methods=["GET", "POST"])
    def material_new():
        if request.method == "POST":
            try:
                cur = conn().execute(
                    "INSERT INTO materials (part_number, name, maker, spec, unit, quantity, "
                    "required_date, budget_unit_price, custom_item, owner_id, "
                    "preferred_supplier_id, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    material_form_values())
                conn().commit()
            except Exception as e:
                flash(f"登録できませんでした: {e}")
                return render_template("material_form.html", m=request.form, **masters())
            price = _float(request.form.get("init_price"))
            lt = _int(request.form.get("init_lead_time"))
            if price is not None or lt is not None:
                db.add_observation(conn(), cur.lastrowid, "manual", price, lt)
            return redirect(url_for("material_detail", mid=cur.lastrowid))
        return render_template("material_form.html", m={}, **masters())

    @app.route("/materials/<int:mid>/edit", methods=["GET", "POST"])
    def material_edit(mid):
        m = get_or_404("materials", mid)
        if request.method == "POST":
            conn().execute(
                "UPDATE materials SET part_number=?, name=?, maker=?, spec=?, unit=?, quantity=?, "
                "required_date=?, budget_unit_price=?, custom_item=?, owner_id=?, "
                "preferred_supplier_id=?, notes=? WHERE id=?", material_form_values() + (mid,))
            conn().commit()
            return redirect(url_for("material_detail", mid=mid))
        return render_template("material_form.html", m=m, **masters())

    @app.post("/materials/<int:mid>/delete")
    def material_delete(mid):
        conn().execute("UPDATE materials SET active=0 WHERE id=?", (mid,))
        conn().commit()
        flash("部材を無効化しました。")
        return redirect(url_for("index"))

    @app.route("/materials/<int:mid>")
    def material_detail(mid):
        c = conn()
        m = get_or_404("materials", mid)
        obs = c.execute(
            "SELECT o.*, s.name AS supplier_name FROM price_observations o "
            "LEFT JOIN suppliers s ON s.id=o.supplier_id WHERE material_id=? "
            "ORDER BY observed_at DESC, o.id DESC", (mid,)).fetchall()
        findings = service.evaluate_material(c, m, settings)
        emails = c.execute("SELECT * FROM emails WHERE material_id=? ORDER BY id DESC",
                           (mid,)).fetchall()
        chart = [{"t": o["observed_at"][:10], "p": o["unit_price"]}
                 for o in reversed(obs) if o["unit_price"] is not None]
        return render_template("material_detail.html", m=m, obs=obs, findings=findings,
                               emails=emails, chart=chart, today=date.today(), **masters())

    @app.post("/materials/<int:mid>/observations")
    def add_observation(mid):
        get_or_404("materials", mid)
        f = request.form
        db.add_observation(conn(), mid, f.get("source") or "manual",
                           _float(f.get("unit_price")), _int(f.get("lead_time_days")),
                           supplier_id=_int(f.get("supplier_id")),
                           stock_qty=_int(f.get("stock_qty")),
                           min_order_qty=_int(f.get("min_order_qty")))
        flash("価格・納期情報を登録しました。")
        return redirect(url_for("material_detail", mid=mid))

    @app.post("/materials/<int:mid>/rfq")
    def make_rfq(mid):
        m = get_or_404("materials", mid)
        sup = get_or_404("suppliers", _int(request.form.get("supplier_id")) or 0)
        eid = service.create_rfq(conn(), m, sup, settings)
        return redirect(url_for("email_edit", eid=eid))

    @app.post("/materials/<int:mid>/order")
    def make_order(mid):
        m = get_or_404("materials", mid)
        f = request.form
        sup = get_or_404("suppliers", _int(f.get("supplier_id")) or 0)
        eid = service.create_order(conn(), m, sup, settings, _int(f.get("quantity")) or m["quantity"],
                                   _float(f.get("unit_price")) or 0,
                                   f.get("delivery_date") or m["required_date"] or "別途ご相談")
        return redirect(url_for("email_edit", eid=eid))

    # ---- CSV 取込 --------------------------------------------------------
    @app.route("/import", methods=["GET", "POST"])
    def import_csv():
        if request.method == "POST":
            file = request.files.get("file")
            text = file.read().decode("utf-8-sig") if file else request.form.get("text", "")
            c = conn()
            ok, missing = 0, []
            sup_id = _int(request.form.get("supplier_id"))
            for r in parse_price_csv(text):
                m = c.execute("SELECT id FROM materials WHERE part_number=?",
                              (r["part_number"],)).fetchone()
                if m is None:
                    missing.append(r["part_number"])
                    continue
                db.add_observation(c, m["id"], "csv", r["unit_price"], r["lead_time_days"],
                                   supplier_id=sup_id, stock_qty=r["stock_qty"],
                                   min_order_qty=r["min_order_qty"], currency=r["currency"],
                                   observed_at=r["observed_at"])
                ok += 1
            flash(f"{ok} 件取り込みました。" +
                  (f" 未登録の品番: {', '.join(missing)}" if missing else ""))
            return redirect(url_for("index"))
        return render_template("import.html", **masters())

    # ---- マスタ (担当者・仕入先) ----------------------------------------
    @app.route("/masters", methods=["GET", "POST"])
    def master_page():
        c = conn()
        if request.method == "POST":
            f = request.form
            if f["type"] == "staff":
                c.execute("INSERT INTO staff (name, email, slack_webhook) VALUES (?,?,?)",
                          (f["name"], f["email"], f.get("slack_webhook") or None))
            else:
                c.execute("INSERT INTO suppliers (name, contact_name, email, auto_send_rfq) "
                          "VALUES (?,?,?,?)", (f["name"], f.get("contact_name") or None,
                                               f["email"], 1 if f.get("auto_send_rfq") else 0))
            c.commit()
            return redirect(url_for("master_page"))
        return render_template("masters.html", **masters())

    @app.post("/suppliers/<int:sid>/toggle-auto")
    def supplier_toggle(sid):
        conn().execute("UPDATE suppliers SET auto_send_rfq = 1 - auto_send_rfq WHERE id=?", (sid,))
        conn().commit()
        return redirect(url_for("master_page"))

    # ---- メール ----------------------------------------------------------
    @app.route("/emails")
    def emails():
        rows = conn().execute(
            "SELECT e.*, m.name AS material_name FROM emails e "
            "LEFT JOIN materials m ON m.id=e.material_id ORDER BY e.id DESC LIMIT 200").fetchall()
        return render_template("emails.html", rows=rows)

    @app.route("/emails/<int:eid>", methods=["GET", "POST"])
    def email_edit(eid):
        e = get_or_404("emails", eid)
        if request.method == "POST":
            f = request.form
            if e["status"] != "sent":
                conn().execute("UPDATE emails SET to_addr=?, cc_addr=?, subject=?, body=? WHERE id=?",
                               (f["to_addr"], f.get("cc_addr") or None, f["subject"], f["body"], eid))
                conn().commit()
            if f.get("action") == "send":
                ok = service.send_email(conn(), eid, settings)
                flash("送信しました。" if ok else "送信に失敗しました。エラー内容を確認してください。")
            elif f.get("action") == "discard":
                conn().execute("DELETE FROM emails WHERE id=? AND status!='sent'", (eid,))
                conn().commit()
                flash("下書きを破棄しました。")
                return redirect(url_for("emails"))
            else:
                flash("保存しました。")
            return redirect(url_for("email_edit", eid=eid))
        return render_template("email_edit.html", e=e)

    @app.post("/alerts/<int:aid>/resolve")
    def alert_resolve(aid):
        conn().execute("UPDATE alerts SET status='resolved' WHERE id=?", (aid,))
        conn().commit()
        return redirect(request.referrer or url_for("index"))

    return app
