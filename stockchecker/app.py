import io
import json
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from markupsafe import Markup

from . import (db, excel, free_sources, jobs, mailflow, orders, pagereader, reminders, service,
               sources, throttle)
from . import inbox as inbox_mod
from .config import Settings
from .providers import parse_price_csv

log = logging.getLogger(__name__)

# 旧互換 (CLI / launcher から参照)
build_providers = sources.build_providers
source_status = sources.source_status

SOURCE_LABELS = {"manual": "手入力", "quote": "見積回答", "csv": "CSV 取込", "demo": "デモ",
                 "page": "ページ監視", "yahoo": "Yahoo!", "rakuten": "楽天", "mouser": "Mouser",
                 "digikey": "Digi-Key", "web": "Web 検索 (AI)", "http": "価格フィード"}
ACTION_LABELS = {"material_new": "部材を登録", "material_edit": "部材を編集", "material_delete": "部材を無効化",
                 "observation": "価格・納期を記録", "watch_add": "監視ページを登録", "fetch_one": "最新価格を取得",
                 "fetch_start": "一括取得を開始", "fetch_done": "一括取得が完了", "email_draft": "メール下書き",
                 "email_sent": "メール送信", "answered": "回答を記録", "comment": "コメント",
                 "confirmed": "確認済みにする", "order_new": "発注を記録", "order_eta": "納期回答を記録",
                 "order_receive": "入荷を記録", "order_cancel": "発注をキャンセル", "mail_received": "返信を受信",
                 "mail_import": "受信メールを取込", "mail_ignore": "受信メールを対応不要に", "excel_import": "Excel 取込",
                 "excel_export": "Excel 出力", "csv_import": "CSV 取込", "settings": "設定を変更",
                 "template": "テンプレートを変更", "staff_new": "担当者を追加", "staff_edit": "担当者を変更",
                 "supplier_new": "仕入先を追加", "supplier_edit": "仕入先を変更", "alert_resolve": "アラートを対応済みに",
                 "email_discard": "下書きを破棄", "watch_remove": "監視ページを外す"}
PERIODS = {"90": "3 か月", "180": "6 か月", "365": "1 年", "0": "全期間"}


def _int(v):
    return int(v) if v not in (None, "") else None


def _float(v):
    return float(str(v).replace(",", "")) if v not in (None, "") else None


def changes_text(old, new, labels):
    """変更点を「項目: 旧 → 新」の文字列にする (履歴用)。"""
    parts = []
    for k, label in labels.items():
        if k not in new:
            continue
        o = old[k] if k in old.keys() else None
        n = new[k]
        if str(o if o is not None else "") != str(n if n is not None else ""):
            parts.append(f"{label}: {o if o not in (None, '') else '(空)'} → {n if n not in (None, '') else '(空)'}")
    return " / ".join(parts)


SETTING_LABELS = {"fetch_default_delay": "アクセス間隔(秒)", "fetch_domain_delays": "サイトごとの間隔",
                  "fetch_max_retries": "再試行回数", "fetch_backoff_seconds": "制限時の待ち時間",
                  "confirm_interval_days": "既定の確認周期", "followup_days": "督促までの日数",
                  "reply_days": "回答期限の日数", "auto_confirm_enabled": "確認メールの自動送信",
                  "auto_followup_enabled": "督促の自動送信", "notify_owner": "担当者への通知"}
MATERIAL_LABELS = {"part_number": "品番", "name": "品名", "maker": "メーカー", "spec": "仕様", "unit": "単位",
                   "quantity": "数量", "required_date": "必要納期", "budget_unit_price": "予算単価",
                   "custom_item": "特注品", "owner_id": "担当者", "preferred_supplier_id": "主仕入先",
                   "notes": "備考", "watch_urls": "商品ページURL", "confirm_interval_days": "確認周期",
                   "auto_confirm": "自動確認メール"}


def yen(v):
    if v is None:
        return "—"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v < 100 else f"{v:,.0f}"


def create_app(settings=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SC"] = settings
    app.jinja_env.filters["fromjson"] = json.loads
    app.jinja_env.filters["yen"] = yen
    app.jinja_env.filters["dict_without"] = lambda d, k: {x: y for x, y in d.items() if x != k}
    app.jinja_env.filters["chartpts"] = lambda series, key: [
        {"t": p["t"], "v": p[key], "s": p.get("v", "")} for p in series if p.get(key) is not None]

    with db.connect(settings.database) as c0:
        db.init_db(c0)
        key = db.get_setting(c0, "secret_key")
        if not key:
            key = secrets.token_hex(32)
            db.set_setting(c0, "secret_key", key)
        # 前回終了時に実行中だった一括取得は中断扱いにする
        c0.execute("UPDATE fetch_jobs SET status='failed', message='アプリの再起動により中断しました', "
                   "finished_at=datetime('now','localtime') WHERE status IN ('running','cancelling')")
        c0.commit()
    app.config["SECRET_KEY"] = key
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

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

    # ================================================================ 操作者 (ログインなし)
    # 全員が同じ権限で使う。履歴に残す名前だけ、ブラウザごとに選んでもらう (Cookie に保存)。
    OPERATOR_COOKIE = "sc_operator"

    @app.before_request
    def _operator():
        g.user, g.actor = None, None
        raw = request.cookies.get(OPERATOR_COOKIE, "")
        if raw.startswith("id:") and raw[3:].isdigit():
            g.user = conn().execute("SELECT * FROM staff WHERE id=?", (int(raw[3:]),)).fetchone()
            g.actor = g.user["name"] if g.user else None
        elif raw.startswith("name:"):
            from urllib.parse import unquote
            g.actor = unquote(raw[5:])[:40] or None
        if settings.csrf and request.method == "POST" and request.endpoint not in ("static",):
            token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not token or token != session.get("csrf"):
                abort(400, "画面を開き直してから、もう一度操作してください (CSRF)")

    def uid():
        return g.user["id"] if g.get("user") else None

    def actor():
        return g.get("actor") or "名前未設定"

    def client():
        return request.remote_addr

    def log_act(action, material_id=None, detail=None):
        db.log_activity(conn(), uid(), action, material_id, detail, actor(), client())

    def kw():
        """記録用の操作者情報 (他モジュールに渡す)。"""
        return {"actor": actor(), "client": client()}

    @app.route("/health")
    def health():
        return "ok"

    @app.post("/operator")
    def set_operator():
        f = request.form
        sid = _int(f.get("staff_id"))
        name = (f.get("name") or "").strip()
        resp = redirect(f.get("back") or request.referrer or url_for("index"))
        from urllib.parse import quote
        if sid:
            value = f"id:{sid}"
        elif name:
            value = "name:" + quote(name[:40])
        else:
            value = "name:" + quote("名前未設定")
        resp.set_cookie(OPERATOR_COOKIE, value, max_age=60 * 60 * 24 * 365, samesite="Lax", httponly=True)
        return resp

    # ================================================================ 共通の表示データ
    def status_pill(s):
        cls, label = {"draft": ("warn", "下書き"), "sent": ("info", "送信済"),
                      "failed": ("crit", "送信失敗")}[s]
        return Markup(f'<span class="pill {cls}">{label}</span>')

    def order_pill(o):
        today = date.today().isoformat()
        due = o["promised_date"] or o["required_date"]
        if o["status"] in orders.OPEN and due and due < today:
            cls, label = "crit", "入荷遅れ"
        else:
            cls, label = {"ordered": ("warn", "納期回答待ち"), "confirmed": ("info", "入荷待ち"),
                          "partial": ("info", "一部入荷"), "received": ("good", "入荷済"),
                          "cancelled": ("off", "キャンセル")}[o["status"]]
        return Markup(f'<span class="pill {cls}">{label}</span>')

    @app.context_processor
    def _ctx():
        c = conn()
        drafts = c.execute("SELECT COUNT(*) FROM emails WHERE status='draft'").fetchone()[0]
        todo = c.execute("SELECT COUNT(DISTINCT a.material_id) FROM alerts a JOIN materials m "
                         "ON m.id=a.material_id WHERE a.status='open' AND m.active=1").fetchone()[0]
        inbox_new = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
        late = c.execute("SELECT COUNT(*) FROM alerts WHERE status='open' AND kind IN ('delivery_late','order_no_eta')").fetchone()[0]
        return {"settings": settings, "sources": sources.source_status(settings),
                "nav_counts": {"drafts": drafts, "todo": todo, "inbox": inbox_new, "orders": late}, "kind_labels": mailflow.KINDS,
                "source_labels": SOURCE_LABELS, "action_labels": ACTION_LABELS,
                "status_pill": status_pill, "order_pill": order_pill, "csrf_token": _csrf_token(), "me": g.get("user"),
                "actor": g.get("actor"), "operator_set": request.cookies.get(OPERATOR_COOKIE) is not None,
                "all_staff_for_operator": c.execute("SELECT id, name FROM staff WHERE active=1 ORDER BY name").fetchall(),
                "running_job": jobs.current_job(c), "is_admin": True,
                "order_labels": orders.STATUS}

    def _csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def masters():
        c = conn()
        return {"staff": c.execute("SELECT * FROM staff WHERE active=1 ORDER BY name").fetchall(),
                "suppliers": c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()}

    def price_series(material_ids, since=None):
        """部材ごとの価格・納期の推移 {id: [{t, p, l, v}]}"""
        if not material_ids:
            return {}
        q = ("SELECT material_id, observed_at, unit_price, lead_time_days, vendor FROM price_observations "
             f"WHERE material_id IN ({','.join('?' * len(material_ids))})")
        args = list(material_ids)
        if since:
            q += " AND observed_at >= ?"
            args.append(since)
        out = {i: [] for i in material_ids}
        for r in conn().execute(q + " ORDER BY observed_at, id", args):
            out[r["material_id"]].append({"t": r["observed_at"][:16], "p": r["unit_price"],
                                          "l": r["lead_time_days"], "v": r["vendor"] or ""})
        return out

    def material_rows(where="m.active=1", args=()):
        """一覧用: 部材 + 最新価格 + 判定 + 状態。"""
        c = conn()
        cfg = mailflow.reminder_config(c)
        mats = c.execute(
            "SELECT m.*, s.name AS owner_name, sp.name AS supplier_name FROM materials m "
            "LEFT JOIN staff s ON s.id=m.owner_id LEFT JOIN suppliers sp ON sp.id=m.preferred_supplier_id "
            f"WHERE {where} ORDER BY m.required_date IS NULL, m.required_date, m.id", args).fetchall()
        open_alerts = {}
        for a in c.execute("SELECT * FROM alerts WHERE status='open'"):
            open_alerts.setdefault(a["material_id"], []).append(a)
        open_orders = {}  # 部材ごとの未入荷の発注
        for o in c.execute(f"SELECT o.* FROM orders o WHERE o.status IN {orders.OPEN} ORDER BY o.id"):
            open_orders.setdefault(o["material_id"], []).append(o)
        pending = {}  # 部材ごとの、回答待ち・下書き中の見積依頼/確認メール
        for e in c.execute(
                "SELECT e.id, e.kind, e.status, em.material_id FROM emails e JOIN email_materials em "
                f"ON em.email_id=e.id WHERE e.kind IN {mailflow.AWAIT_REPLY} AND e.answered_at IS NULL "
                "AND e.status IN ('draft','sent') ORDER BY e.id"):
            pending[e["material_id"]] = e
        rows = []
        for m in mats:
            latest = db.latest_observation(c, m["id"])
            findings = service.evaluate_material(c, m, settings)
            nq = any(f.needs_quote for f in findings)
            prev = db.previous_observation(c, m["id"], latest["id"]) if latest else None
            delta = None
            if latest and prev and latest["unit_price"] is not None and prev["unit_price"]:
                d = (latest["unit_price"] - prev["unit_price"]) / prev["unit_price"] * 100
                delta = d if abs(d) >= 0.05 else None
            days_left = None
            if m["required_date"]:
                try:
                    days_left = (date.fromisoformat(m["required_date"]) - date.today()).days
                except ValueError:
                    pass
            interval, base, due = reminders.confirm_status(c, m, cfg)
            reminders_ = [a for a in open_alerts.get(m["id"], []) if a["kind"] in reminders.REMINDER_KINDS]
            status = "quote" if nq else ("warn" if findings or reminders_ else "ok")
            rows.append({"m": m, "latest": latest, "findings": findings, "needs_quote": nq,
                         "status": status, "delta": delta, "days_left": days_left,
                         "method": "web" if (m["watch_urls"] or "").strip() else "mail",
                         "confirm_due": due, "confirm_interval": interval, "reminders": reminders_,
                         "pending": pending.get(m["id"]), "orders": open_orders.get(m["id"], [])})
        return rows

    # ================================================================ ダッシュボード
    @app.route("/")
    def index():
        c = conn()
        mine = request.args.get("mine") == "1"
        rows = material_rows("m.active=1 AND m.owner_id=?", (uid(),)) if mine else material_rows()
        quote = [r for r in rows if r["needs_quote"] and not r["orders"]]  # 発注済みのものは除く
        confirm = [r for r in rows for a in r["reminders"] if a["kind"] == "confirm_due"]
        noreply = [(r, a) for r in rows for a in r["reminders"] if a["kind"] == "no_reply"]
        order_alerts = [(r, a) for r in rows for a in r["reminders"] if a["kind"] in orders.ORDER_ALERTS]
        n_open_orders = c.execute(f"SELECT COUNT(*) FROM orders WHERE status IN {orders.OPEN}").fetchone()[0]
        n_inbox = c.execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]
        drafts = c.execute("SELECT e.*, s.name AS supplier_name FROM emails e LEFT JOIN suppliers s "
                           "ON s.id=e.supplier_id WHERE e.status='draft' ORDER BY e.id DESC LIMIT 20").fetchall()
        last_job = c.execute("SELECT j.*, COALESCE(j.started_by_name, s.name) AS user_name FROM fetch_jobs j LEFT JOIN staff s "
                             "ON s.id=j.started_by ORDER BY j.id DESC LIMIT 1").fetchone()
        job_errors = c.execute("SELECT i.*, m.part_number, m.name FROM fetch_job_items i JOIN materials m "
                               "ON m.id=i.material_id WHERE i.job_id=? AND i.status='error'",
                               (last_job["id"],)).fetchall() if last_job else []
        activity = c.execute("SELECT a.*, COALESCE(a.actor, s.name) AS user_name, m.part_number FROM activity a "
                             "LEFT JOIN staff s ON s.id=a.user_id LEFT JOIN materials m ON m.id=a.material_id "
                             "ORDER BY a.id DESC LIMIT 12").fetchall()
        web_targets = sum(1 for r in rows if r["method"] == "web")
        return render_template("index.html", rows=rows, quote=quote, confirm=confirm, noreply=noreply,
                               order_alerts=order_alerts, n_open_orders=n_open_orders, n_inbox=n_inbox,
                               drafts=drafts, last_job=last_job, job_errors=job_errors,
                               activity=activity, mine=mine, web_targets=web_targets)

    # ================================================================ 一括取得
    @app.post("/fetch")
    def fetch_start():
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()] or None
        job_id, new = jobs.start(settings, uid(), ids, run_async=settings.run_jobs_async, actor=actor())
        if not new:
            flash("別の一括取得が実行中です。完了までお待ちください。", "error")
        return redirect(request.form.get("back") or url_for("index"))

    @app.route("/api/fetch/<int:job_id>")
    def fetch_status(job_id):
        j = get_or_404("fetch_jobs", job_id)
        return jsonify({k: j[k] for k in j.keys()})

    @app.post("/fetch/<int:job_id>/cancel")
    def fetch_cancel(job_id):
        jobs.cancel(conn(), job_id)
        flash("一括取得を中止しています (今取得中の部材が終わると止まります)。")
        return redirect(request.referrer or url_for("index"))

    @app.route("/fetch/<int:job_id>")
    def fetch_result(job_id):
        j = conn().execute("SELECT j.*, COALESCE(j.started_by_name, s.name) AS user_name FROM fetch_jobs j LEFT JOIN staff s "
                           "ON s.id=j.started_by WHERE j.id=?", (job_id,)).fetchone() or abort(404)
        items = conn().execute("SELECT i.*, m.part_number, m.name FROM fetch_job_items i "
                               "LEFT JOIN materials m ON m.id=i.material_id WHERE job_id=? "
                               "ORDER BY i.status='ok', i.id", (job_id,)).fetchall()
        return render_template("fetch_result.html", j=j, items=items)

    # ================================================================ 部材
    @app.route("/materials")
    def materials():
        rows = material_rows()
        series = price_series([r["m"]["id"] for r in rows])
        spark = {i: [p for p in s if p["p"] is not None][-20:] for i, s in series.items()}
        return render_template("materials.html", rows=rows, spark=spark, **masters())

    @app.post("/materials/bulk")
    def materials_bulk():
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        action = request.form.get("action")
        if not ids:
            flash("部材を選択してください。", "error")
            return redirect(url_for("materials"))
        if action == "fetch":
            _, new = jobs.start(settings, uid(), ids, run_async=settings.run_jobs_async, actor=actor())
            if not new:
                flash("別の一括取得が実行中です。", "error")
            return redirect(url_for("index"))
        if action in ("rfq", "confirm", "order"):
            mats = conn().execute(f"SELECT * FROM materials WHERE id IN ({','.join('?' * len(ids))})",
                                  ids).fetchall()
            eids, missing = mailflow.create_bulk(conn(), action, mats, settings, uid(), **kw())
            if missing:
                flash("主仕入先が未設定のため作成できなかった部材: "
                      + ", ".join(m["part_number"] for m in missing), "error")
            if eids:
                flash(f"{mailflow.KINDS[action]}の下書きを仕入先ごとに {len(eids)} 通作成しました。")
                return redirect(url_for("email_edit", eid=eids[0]) if len(eids) == 1 else url_for("emails"))
        return redirect(url_for("materials"))

    def material_form_values():
        f = request.form
        ci = f.get("confirm_interval_days", "")
        return (f["part_number"].strip(), f["name"].strip(), f.get("maker") or None,
                f.get("spec") or None, f.get("unit") or "個", _int(f.get("quantity")) or 1,
                f.get("required_date") or None, _float(f.get("budget_unit_price")),
                1 if f.get("custom_item") else 0, _int(f.get("owner_id")),
                _int(f.get("preferred_supplier_id")), f.get("notes") or None,
                (f.get("watch_urls") or "").strip() or None,
                None if ci in ("", "default") else int(ci), 1 if f.get("auto_confirm") else 0)

    COLS = ("part_number, name, maker, spec, unit, quantity, required_date, budget_unit_price, "
            "custom_item, owner_id, preferred_supplier_id, notes, watch_urls, confirm_interval_days, auto_confirm")

    @app.route("/materials/new", methods=["GET", "POST"])
    def material_new():
        if request.method == "POST":
            try:
                cur = conn().execute(f"INSERT INTO materials ({COLS}, created_by) VALUES "
                                     f"({','.join('?' * 16)})", material_form_values() + (uid(),))
                conn().commit()
            except Exception as e:
                flash(f"登録できませんでした: {e}", "error")
                return render_template("material_form.html", m=request.form, **masters())
            mid = cur.lastrowid
            price = _float(request.form.get("init_price"))
            wurl = (request.form.get("watch_urls") or "").strip().splitlines()
            lt = _int(request.form.get("init_lead_time"))
            if price is not None or lt is not None:
                db.add_observation(conn(), mid, "page" if wurl else "manual", price, lt,
                                   vendor=pagereader.domain(wurl[0]) if wurl else None,
                                   url=wurl[0].strip() if wurl else None, created_by=uid())
            log_act("material_new", mid, request.form["part_number"])
            return redirect(url_for("material_detail", mid=mid))
        prefill = {"owner_id": uid()}
        url = request.args.get("url", "").strip()
        if url:
            try:
                info = pagereader.read(url, _hint_for(url))
                prefill.update({"name": info.title, "part_number": info.part_number, "maker": info.maker,
                                "watch_urls": url, "init_price": pagereader.net_price(info),
                                "init_lead_time": info.lead_time_days})
                flash("商品ページから入力しました。品番・品名を確認して保存してください。"
                      + ("" if info.unit_price is not None else
                         " 価格は自動で読み取れなかったため、保存後に部材画面で価格の場所を指定してください。"))
            except pagereader.FetchError as e:
                flash(f"ページを開けませんでした: {e}", "error")
                prefill["watch_urls"] = url
        return render_template("material_form.html", m=prefill, **masters())

    @app.route("/materials/<int:mid>/edit", methods=["GET", "POST"])
    def material_edit(mid):
        m = get_or_404("materials", mid)
        if request.method == "POST":
            cols = [c.strip() for c in COLS.split(",")]
            vals = material_form_values()
            sets = ", ".join(f"{c}=?" for c in cols)
            conn().execute(f"UPDATE materials SET {sets}, updated_by=?, updated_at=datetime('now','localtime') "
                           "WHERE id=?", vals + (uid(), mid))
            conn().commit()
            new = dict(zip(cols, vals))
            names = {r["id"]: r["name"] for r in conn().execute("SELECT id, name FROM staff")}
            sups = {r["id"]: r["name"] for r in conn().execute("SELECT id, name FROM suppliers")}
            old = dict(m)
            for d, key in ((names, "owner_id"), (sups, "preferred_supplier_id")):
                old[key], new[key] = d.get(old[key]), d.get(new[key])
            for key in ("custom_item", "auto_confirm"):
                old[key], new[key] = ("はい" if old[key] else "いいえ"), ("はい" if new[key] else "いいえ")
            log_act("material_edit", mid, changes_text(old, new, MATERIAL_LABELS) or "変更なし")
            return redirect(url_for("material_detail", mid=mid))
        return render_template("material_form.html", m=m, **masters())

    @app.post("/materials/<int:mid>/delete")
    def material_delete(mid):
        conn().execute("UPDATE materials SET active=0 WHERE id=?", (mid,))
        conn().commit()
        log_act("material_delete", mid)
        flash("部材を無効化しました。")
        return redirect(url_for("materials"))

    @app.route("/materials/<int:mid>")
    def material_detail(mid):
        c = conn()
        m = get_or_404("materials", mid)
        obs = c.execute(
            "SELECT o.*, s.name AS supplier_name, u.name AS user_name FROM price_observations o "
            "LEFT JOIN suppliers s ON s.id=o.supplier_id LEFT JOIN staff u ON u.id=o.created_by "
            "WHERE material_id=? ORDER BY observed_at DESC, o.id DESC", (mid,)).fetchall()
        findings = service.evaluate_material(c, m, settings)
        emails = c.execute("SELECT DISTINCT e.* FROM emails e LEFT JOIN email_materials em ON em.email_id=e.id "
                           "WHERE e.material_id=? OR em.material_id=? ORDER BY e.id DESC", (mid, mid)).fetchall()
        waiting = [e for e in emails if e["status"] == "sent" and e["kind"] in mailflow.AWAIT_REPLY
                   and not e["answered_at"]]
        timeline = c.execute(
            "SELECT 'act' AS type, a.created_at, a.action, a.detail AS body, COALESCE(a.actor, s.name) AS user_name "
            "FROM activity a LEFT JOIN staff s ON s.id=a.user_id WHERE a.material_id=? "
            "UNION ALL SELECT 'comment', c.created_at, 'comment', c.body, COALESCE(c.actor, s.name) FROM comments c "
            "LEFT JOIN staff s ON s.id=c.user_id WHERE c.material_id=? ORDER BY 2 DESC LIMIT 60",
            (mid, mid)).fetchall()
        cfg = mailflow.reminder_config(c)
        interval, base, due = reminders.confirm_status(c, m, cfg)
        alerts = c.execute("SELECT * FROM alerts WHERE material_id=? AND status='open'", (mid,)).fetchall()
        series = price_series([mid])[mid]
        m_orders = orders.listing(c, "o.material_id=?", (mid,))
        return render_template("material_detail.html", m_orders=m_orders, m=m, obs=obs, findings=findings, emails=emails,
                               waiting=waiting, timeline=timeline, series=series, alerts=alerts,
                               confirm=(interval, base, due), cfg=cfg, today=date.today(), **masters())

    @app.post("/materials/<int:mid>/observations")
    def add_observation(mid):
        get_or_404("materials", mid)
        f = request.form
        src = f.get("source") or "manual"
        db.add_observation(conn(), mid, src, _float(f.get("unit_price")), _int(f.get("lead_time_days")),
                           supplier_id=_int(f.get("supplier_id")), stock_qty=_int(f.get("stock_qty")),
                           min_order_qty=_int(f.get("min_order_qty")), created_by=uid())
        if src == "quote":
            mailflow.record_quote_answer(conn(), mid, uid(), **kw())
        log_act("observation", mid, f"単価 {f.get('unit_price') or '—'} / 納期 {f.get('lead_time_days') or '—'} 日"
                + (" (見積回答)" if src == "quote" else ""))
        flash("価格・納期を記録しました。" + (" 回答待ちのメールを回答済みにしました。" if src == "quote" else ""))
        return redirect(url_for("material_detail", mid=mid))

    @app.post("/materials/<int:mid>/confirmed")
    def mark_confirmed(mid):
        get_or_404("materials", mid)
        mailflow.record_quote_answer(conn(), mid, uid(), **kw())
        log_act("confirmed", mid, request.form.get("note") or "電話などで確認済み")
        flash("確認済みにしました。次回の確認期限を更新しました。")
        return redirect(url_for("material_detail", mid=mid))

    @app.post("/materials/<int:mid>/comment")
    def add_comment(mid):
        get_or_404("materials", mid)
        body = request.form.get("body", "").strip()
        if body:
            conn().execute("INSERT INTO comments (material_id, user_id, body, actor) VALUES (?,?,?,?)", (mid, uid(), body, actor()))
            conn().commit()
        return redirect(url_for("material_detail", mid=mid) + "#timeline")

    @app.post("/materials/<int:mid>/fetch")
    def fetch_now(mid):
        m = get_or_404("materials", mid)
        f = jobs.Fetcher(conn(), settings)
        try:
            with pagereader.BrowserSession() as s:
                f.session = s
                status, best, msgs = f.fetch_material(m, user_id=uid())
        except Exception as e:
            status, best, msgs = "error", None, [str(e)]
        if status == "ok":
            flash(f"最新の価格・納期を取得しました: 単価 ¥{yen(best.get('unit_price'))} / "
                  f"納期 {best.get('lead_time_days') if best.get('lead_time_days') is not None else '—'} 日")
            log_act("fetch_one", mid)
            service.run_checks(conn(), settings, notify=False)
        for msg in msgs:
            flash(("取得できませんでした: " if status != "ok" else "一部の取得元でエラー: ") + msg,
                  "error" if status != "skipped" else "message")
        return redirect(url_for("material_detail", mid=mid))

    # ---- 商品ページ URL
    def _add_watch_url(mid, url):
        m = get_or_404("materials", mid)
        urls = [u.strip() for u in (m["watch_urls"] or "").splitlines() if u.strip()]
        if url not in urls:
            urls.append(url)
        conn().execute("UPDATE materials SET watch_urls=? WHERE id=?", ("\n".join(urls), mid))
        conn().commit()
        log_act("watch_add", mid, pagereader.domain(url))

    def _record_page(m, info, method):
        offer = free_sources.info_to_offer(info, m["quantity"] or 1)
        offer["note"] = method
        db.add_observation(conn(), m["id"], "page", unit_price=offer["unit_price"],
                           lead_time_days=offer["lead_time_days"], stock_qty=offer["stock_qty"],
                           vendor=offer["vendor"], url=offer["url"],
                           detail=json.dumps([offer], ensure_ascii=False), created_by=uid())
        return offer

    def _save_debug(info):
        """読み取れなかったページの HTML を保存し、パスを返す (問い合わせ用)。"""
        if not info or not getattr(info, "html", None):
            return None
        from pathlib import Path
        d = Path(settings.database).resolve().parent / "debug"
        d.mkdir(exist_ok=True)
        path = d / f"{pagereader.domain(info.url).replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.html"
        path.write_text(info.html, encoding="utf-8")
        return str(path)

    def _hint_for(url):
        r = conn().execute("SELECT * FROM site_hints WHERE domain=?", (pagereader.domain(url),)).fetchone()
        return dict(r) if r else None

    @app.post("/materials/<int:mid>/watch")
    def watch_url(mid):
        m = get_or_404("materials", mid)
        url = request.form.get("url", "").strip()
        try:
            info = pagereader.read(url, _hint_for(url))
        except pagereader.FetchError as e:
            flash(f"ページを開けませんでした: {e}", "error")
            return redirect(url_for("material_detail", mid=mid))
        if info.unit_price is None or info.is_group:
            return render_template("watch_pick.html", m=m, info=info, url=url, debug_path=_save_debug(info))
        _add_watch_url(mid, url)
        o = _record_page(m, info, info.method)
        flash(f"追跡を開始しました: 単価 ¥{yen(o['unit_price'])}"
              f"{' (税込から換算)' if info.tax_included else ''} / 読み取り方法: {info.method}。"
              "違う金額の場合は「価格の場所を指定し直す」を押してください。")
        return redirect(url_for("material_detail", mid=mid))

    @app.route("/materials/<int:mid>/watch/pick", methods=["GET", "POST"])
    def watch_pick(mid):
        m = get_or_404("materials", mid)
        if request.method == "GET":  # 指定し直し
            url = request.args.get("url", "")
            try:
                info = pagereader.read(url)
            except pagereader.FetchError as e:
                flash(f"ページを開けませんでした: {e}", "error")
                return redirect(url_for("material_detail", mid=mid))
            return render_template("watch_pick.html", m=m, info=info, url=url)
        f = request.form
        url = f["url"]
        manual = _float(f.get("manual_price"))
        info = pagereader.PageInfo(url=url, lead_time_days=_int(f.get("lead_time_days")))
        if manual is not None:
            info.unit_price, info.tax_included = manual, f.get("manual_tax") == "incl"
            method = "手入力"
        else:
            ch = json.loads(f["choice"])
            info.unit_price, info.tax_included = ch["value"], f.get("tax") == "incl"
            if ch.get("label"):
                conn().execute("INSERT OR REPLACE INTO site_hints (domain, label, tax) VALUES (?,?,?)",
                               (pagereader.domain(url), ch["label"], f.get("tax") or None))
                conn().commit()
            method = f"見出し「{ch.get('label') or '—'}」"
        _add_watch_url(mid, url)
        o = _record_page(m, info, method)
        flash(f"追跡を開始しました: 単価 ¥{yen(o['unit_price'])}。"
              + ("次回からこのサイトは同じ見出しの金額を自動で読み取ります。" if manual is None else ""))
        return redirect(url_for("material_detail", mid=mid))

    @app.post("/materials/<int:mid>/watch/remove")
    def watch_remove(mid):
        m = get_or_404("materials", mid)
        url = request.form.get("url", "")
        urls = [u.strip() for u in (m["watch_urls"] or "").splitlines() if u.strip() and u.strip() != url]
        conn().execute("UPDATE materials SET watch_urls=? WHERE id=?", ("\n".join(urls) or None, mid))
        conn().commit()
        log_act("watch_remove", mid, url)
        flash("監視ページを外しました。")
        return redirect(url_for("material_detail", mid=mid))

    # ---- ワンタッチでメール下書き
    @app.post("/materials/<int:mid>/mail/<kind>")
    def material_mail(mid, kind):
        if kind not in ("rfq", "confirm", "order", "followup"):
            abort(404)
        m = get_or_404("materials", mid)
        f = request.form
        if kind == "followup":
            e = get_or_404("emails", _int(f.get("email_id")) or 0)
            eid = mailflow.create_followup(conn(), e, settings, uid(), **kw())
            return redirect(url_for("email_edit", eid=eid))
        sid = _int(f.get("supplier_id")) or m["preferred_supplier_id"]
        if not sid:
            flash("主仕入先が未設定です。部材の編集で主仕入先を選ぶか、送り先を選んでください。", "error")
            return redirect(url_for("material_detail", mid=mid))
        sup = get_or_404("suppliers", sid)
        if kind == "order":
            last = db.latest_observation(conn(), mid)
            qty = _int(f.get("quantity")) or m["quantity"]
            price = _float(f.get("unit_price"))
            if price is None and last:
                price = last["unit_price"]
            eid = service.create_order(conn(), m, sup, settings, qty, price or 0,
                                       f.get("delivery_date") or m["required_date"] or "別途ご相談", uid(), **kw())
        else:
            eid = mailflow.create(conn(), kind, sup, [m], settings, uid(), **kw())
        return redirect(url_for("email_edit", eid=eid))

    # ================================================================ 価格推移 (グラフ)
    @app.route("/charts")
    def charts():
        period = request.args.get("period", "180")
        if period not in PERIODS:
            period = "180"
        owner = _int(request.args.get("owner"))
        q = request.args.get("q", "").strip()
        where, args = "m.active=1", []
        if owner:
            where += " AND m.owner_id=?"
            args.append(owner)
        if q:
            where += " AND (m.part_number LIKE ? OR m.name LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        mats = conn().execute(f"SELECT m.*, s.name AS owner_name FROM materials m LEFT JOIN staff s "
                              f"ON s.id=m.owner_id WHERE {where} ORDER BY m.part_number", args).fetchall()
        since = (datetime.now() - timedelta(days=int(period))).strftime("%Y-%m-%d") if period != "0" else None
        series = price_series([m["id"] for m in mats], since)
        cards = []
        for m in mats:
            pts = [p for p in series[m["id"]] if p["p"] is not None]
            first, last = (pts[0]["p"], pts[-1]["p"]) if pts else (None, None)
            change = (last - first) / first * 100 if pts and first else None
            cards.append({"m": m, "pts": pts, "lead": [p for p in series[m["id"]] if p["l"] is not None],
                          "first": first, "last": last, "change": change,
                          "lo": min(p["p"] for p in pts) if pts else None,
                          "hi": max(p["p"] for p in pts) if pts else None})
        ranked = sorted([c for c in cards if c["change"] is not None and len(c["pts"]) > 1],
                        key=lambda c: -abs(c["change"]))[:12]
        return render_template("charts.html", cards=cards, ranked=ranked, period=period, periods=PERIODS,
                               owner=owner, q=q, **masters())

    # ================================================================ CSV 取込
    @app.route("/import", methods=["GET", "POST"])
    def import_csv():
        if request.method == "POST":
            file = request.files.get("file")
            text = file.read().decode("utf-8-sig") if file and file.filename else request.form.get("text", "")
            c = conn()
            ok, missing = 0, []
            sup_id = _int(request.form.get("supplier_id"))
            for r in parse_price_csv(text):
                m = c.execute("SELECT id FROM materials WHERE part_number=?", (r["part_number"],)).fetchone()
                if m is None:
                    missing.append(r["part_number"])
                    continue
                db.add_observation(c, m["id"], "csv", r["unit_price"], r["lead_time_days"],
                                   supplier_id=sup_id, stock_qty=r["stock_qty"],
                                   min_order_qty=r["min_order_qty"], currency=r["currency"],
                                   observed_at=r["observed_at"], created_by=uid())
                ok += 1
            log_act("csv_import", None, f"価格表 CSV を {ok} 件取り込み")
            flash(f"{ok} 件取り込みました。" + (f" 未登録の品番: {', '.join(missing)}" if missing else ""))
            return redirect(url_for("materials"))
        return render_template("import.html", **masters())

    # ================================================================ 担当者 (チーム)・仕入先
    @app.route("/masters", methods=["GET", "POST"])
    def master_page():
        c = conn()
        if request.method == "POST":
            f = request.form
            if f["type"] == "staff":
                c.execute("INSERT INTO staff (name, email, slack_webhook) VALUES (?,?,?)",
                          (f["name"], f["email"], f.get("slack_webhook") or None))
                log_act("staff_new", None, f"{f['name']} ({f['email']})")
                flash(f"{f['name']} さんを担当者に追加しました。")
            else:
                c.execute("INSERT INTO suppliers (name, contact_name, email, auto_send_rfq) VALUES (?,?,?,?)",
                          (f["name"], f.get("contact_name") or None, f["email"], 1 if f.get("auto_send_rfq") else 0))
                log_act("supplier_new", None, f"{f['name']} ({f['email']})")
            c.commit()
            return redirect(url_for("master_page"))
        all_staff = c.execute("SELECT * FROM staff ORDER BY active DESC, name").fetchall()
        return render_template("masters.html", all_staff=all_staff, **masters())

    @app.post("/staff/<int:sid>")
    def staff_update(sid):
        s = get_or_404("staff", sid)
        f = request.form
        if f.get("action") == "toggle":
            conn().execute("UPDATE staff SET active=1-active WHERE id=?", (sid,))
            log_act("staff_edit", None, f"{s['name']} を{'無効' if s['active'] else '有効'}にしました")
        else:
            new = {"name": f["name"], "email": f["email"], "slack_webhook": f.get("slack_webhook") or None}
            conn().execute("UPDATE staff SET name=?, email=?, slack_webhook=? WHERE id=?",
                           (new["name"], new["email"], new["slack_webhook"], sid))
            ch = changes_text(s, new, {"name": "氏名", "email": "メール", "slack_webhook": "チャット通知"})
            if ch:
                log_act("staff_edit", None, f"{s['name']}: {ch}")
        conn().commit()
        return redirect(url_for("master_page"))

    @app.post("/suppliers/<int:sid>")
    def supplier_update(sid):
        s = get_or_404("suppliers", sid)
        f = request.form
        if f.get("action") == "toggle-auto":
            conn().execute("UPDATE suppliers SET auto_send_rfq = 1 - auto_send_rfq WHERE id=?", (sid,))
            log_act("supplier_edit", None, f"{s['name']}: 見積依頼の自動送信を{'OFF' if s['auto_send_rfq'] else 'ON'}")
        else:
            new = {"name": f["name"], "contact_name": f.get("contact_name") or None, "email": f["email"]}
            conn().execute("UPDATE suppliers SET name=?, contact_name=?, email=? WHERE id=?",
                           (new["name"], new["contact_name"], new["email"], sid))
            ch = changes_text(s, new, {"name": "社名", "contact_name": "ご担当者", "email": "メール"})
            if ch:
                log_act("supplier_edit", None, f"{s['name']}: {ch}")
        conn().commit()
        return redirect(url_for("master_page"))

    # ================================================================ メール
    @app.route("/emails")
    def emails():
        tab = request.args.get("tab", "draft")
        where = {"draft": "e.status IN ('draft','failed')",
                 "waiting": f"e.status='sent' AND e.kind IN {mailflow.AWAIT_REPLY} AND e.answered_at IS NULL",
                 "sent": "e.status='sent'", "all": "1=1"}.get(tab, "1=1")
        rows = conn().execute(
            "SELECT e.*, s.name AS supplier_name, u.name AS user_name, "
            "(SELECT group_concat(m.part_number, ', ') FROM email_materials em JOIN materials m "
            " ON m.id=em.material_id WHERE em.email_id=e.id) AS parts, "
            "(SELECT COUNT(*) FROM emails f WHERE f.followup_of=e.id) AS followups "
            "FROM emails e LEFT JOIN suppliers s ON s.id=e.supplier_id LEFT JOIN staff u ON u.id=e.created_by "
            f"WHERE {where} ORDER BY e.id DESC LIMIT 300").fetchall()
        counts = {t: conn().execute(f"SELECT COUNT(*) FROM emails e WHERE {w}").fetchone()[0] for t, w in {
            "draft": "e.status IN ('draft','failed')",
            "waiting": f"e.status='sent' AND e.kind IN {mailflow.AWAIT_REPLY} AND e.answered_at IS NULL"}.items()}
        followup_days = int(mailflow.reminder_config(conn())["followup_days"])
        overdue = (datetime.now() - timedelta(days=followup_days)).strftime("%Y-%m-%d %H:%M:%S")
        return render_template("emails.html", rows=rows, tab=tab, counts=counts, overdue=overdue)

    @app.route("/emails/<int:eid>", methods=["GET", "POST"])
    def email_edit(eid):
        e = get_or_404("emails", eid)
        if request.method == "POST":
            f = request.form
            if e["status"] != "sent":
                conn().execute("UPDATE emails SET to_addr=?, cc_addr=?, subject=?, body=? WHERE id=?",
                               (f["to_addr"], f.get("cc_addr") or None, f["subject"], f["body"], eid))
                conn().commit()
            action = f.get("action")
            if action == "send":
                ok = service.send_email(conn(), eid, settings, uid(), **kw())
                flash("送信しました。" + ("" if settings.smtp_host else
                                       f" (SMTP 未設定のため {settings.outbox_dir} に .eml として保存しました)")
                      if ok else "送信に失敗しました。エラー内容を確認してください。", "message" if ok else "error")
            elif action == "discard":
                linked = [m["id"] for m in mailflow.linked_materials(conn(), eid)] or [e["material_id"]]
                conn().execute("DELETE FROM emails WHERE id=? AND status!='sent'", (eid,))
                conn().commit()
                for mid_ in linked:
                    log_act("email_discard", mid_, f"下書きを破棄「{e['subject'][:50]}」")
                flash("下書きを破棄しました。")
                return redirect(url_for("emails"))
            elif action == "answered":
                mailflow.mark_answered(conn(), eid, uid(), **kw())
                flash("回答ありとして記録しました。価格・納期が届いた場合は部材画面で「見積回答」を記録してください。")
            elif action == "followup":
                fid = mailflow.create_followup(conn(), e, settings, uid(), **kw())
                return redirect(url_for("email_edit", eid=fid))
            else:
                flash("保存しました。")
            return redirect(url_for("email_edit", eid=eid))
        mats = mailflow.linked_materials(conn(), eid)
        orig = conn().execute("SELECT * FROM emails WHERE id=?", (e["followup_of"],)).fetchone() if e["followup_of"] else None
        author = conn().execute("SELECT name FROM staff WHERE id=?", (e["created_by"],)).fetchone() if e["created_by"] else None
        return render_template("email_edit.html", e=e, mats=mats, orig=orig, author=author)

    @app.post("/alerts/<int:aid>/resolve")
    def alert_resolve(aid):
        a = get_or_404("alerts", aid)
        conn().execute("UPDATE alerts SET status='resolved' WHERE id=?", (aid,))
        log_act("alert_resolve", a["material_id"], a["message"][:80])
        conn().commit()
        return redirect(request.referrer or url_for("index"))

    # ================================================================ 設定 (管理者)
    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        c = conn()
        if request.method == "POST":
            f = request.form
            keys = list(jobs.FETCH_DEFAULTS) + list(mailflow.REMINDER_DEFAULTS)
            before = {**jobs.fetch_config(c), **mailflow.reminder_config(c)}
            for k in keys:
                if k in f:
                    v = f.get(k, "").strip()
                    if k != "fetch_domain_delays":
                        v = str(max(0, int(float(v or 0))))
                    db.set_setting(c, k, v)
                elif k in ("auto_confirm_enabled", "auto_followup_enabled", "notify_owner"):
                    db.set_setting(c, k, "0")
            after = {**jobs.fetch_config(c), **mailflow.reminder_config(c)}
            ch = changes_text(before, after, SETTING_LABELS)
            if ch:
                log_act("settings", None, ch)
            flash("設定を保存しました。")
            return redirect(url_for("settings_page"))
        return render_template("settings.html", fetch=jobs.fetch_config(c), rem=mailflow.reminder_config(c),
                               delays=throttle.parse_domain_delays(jobs.fetch_config(c)["fetch_domain_delays"]),
                               lan_urls=settings.lan_urls)

    @app.route("/settings/templates", methods=["GET", "POST"])
    def templates_page():
        c = conn()
        kind = request.args.get("kind", "rfq")
        if kind not in mailflow.DEFAULT_TEMPLATES:
            kind = "rfq"
        if request.method == "POST":
            if request.form.get("action") == "reset":
                c.execute("DELETE FROM mail_templates WHERE kind=?", (kind,))
                log_act("template", None, f"{mailflow.KINDS[kind]}のテンプレートを既定に戻しました")
                flash("既定の文面に戻しました。")
            else:
                c.execute("INSERT OR REPLACE INTO mail_templates (kind, subject, body, updated_by, updated_at) "
                          "VALUES (?,?,?,?,datetime('now','localtime'))",
                          (kind, request.form["subject"], request.form["body"], uid()))
                log_act("template", None, f"{mailflow.KINDS[kind]}のテンプレートを変更しました")
                flash("テンプレートを保存しました。")
            c.commit()
            return redirect(url_for("templates_page", kind=kind))
        subject, body = mailflow.get_template(c, kind)
        custom = c.execute("SELECT t.*, s.name AS user_name FROM mail_templates t LEFT JOIN staff s "
                           "ON s.id=t.updated_by WHERE kind=?", (kind,)).fetchone()
        return render_template("templates.html", kind=kind, subject=subject, body=body, custom=custom,
                               placeholders=mailflow.PLACEHOLDERS)

    @app.route("/sources")
    def sources_page():
        s = settings
        info = [
            dict(name="商品ページ監視", free=True, on=s.page_watch,
                 desc="部材ごとに登録した通販サイトの商品ページから価格・在庫・納期を読み取ります。",
                 fit="モノタロウ・ミスミ・アスクル・メーカー直販など、商品ページが決まっている部材",
                 how="部材画面で URL を貼り付け (page_watch = 1)"),
            dict(name="Yahoo!ショッピング", free=True, on=bool(s.yahoo_app_id),
                 desc="品番で商品検索し、商品名に品番を含む出品の最安値を取得します (税込→税抜換算)。",
                 fit="汎用品・工具・消耗品・型番のある機器",
                 how='<a href="https://e.developer.yahoo.co.jp/" target="_blank">Yahoo!デベロッパー</a>でアプリ登録 → yahoo_app_id'),
            dict(name="楽天市場", free=True, on=bool(s.rakuten_app_id),
                 desc="品番で商品検索し、商品名に品番を含む出品の最安値を取得します。",
                 fit="汎用品・工具・消耗品・型番のある機器",
                 how='<a href="https://webservice.rakuten.co.jp/" target="_blank">楽天ウェブサービス</a>でアプリ登録 → rakuten_app_id'),
            dict(name="Mouser", free=True, on=bool(s.mouser_api_key),
                 desc="品番完全一致で価格 (数量割引)・在庫・メーカー納期を取得します。",
                 fit="電子部品", how='<a href="https://www.mouser.jp/api-hub/" target="_blank">Mouser API Hub</a> → mouser_api_key'),
            dict(name="Digi-Key", free=True, on=bool(s.digikey_client_id and s.digikey_client_secret),
                 desc="品番完全一致で価格 (数量割引)・在庫・メーカー納期を取得します。",
                 fit="電子部品", how='<a href="https://developer.digikey.com/" target="_blank">Digi-Key Developer</a> → digikey_client_id / secret'),
            dict(name="Web 検索 (AI)", free=False, on=bool(s.anthropic_api_key) and s.web_search != "off",
                 desc="AI が Web を検索して相場を調べます。URL の無い部材の最後の手段です。",
                 fit="何でも (鋼材・機構部品・特殊品など)", how="anthropic_api_key と web_search = fallback"),
        ]
        return render_template("sources.html", source_info=info, test=request.args.get("test_url"))

    @app.post("/sources/test")
    def sources_test():
        url = request.form.get("url", "").strip()
        try:
            i = pagereader.read(url, _hint_for(url))
            if i.is_group:
                flash("このページはサイズ違いをまとめた一覧ページです。目的のサイズを選んで開いた"
                      "商品ページ (モノタロウなら /p/ で始まる URL) を貼り付けてください。", "error")
            elif i.unit_price is not None:
                flash(f"読み取り成功: 単価 ¥{yen(i.unit_price)} ({i.method}{' / ブラウザ表示' if i.rendered else ''}) / "
                      f"納期 {i.lead_time_days if i.lead_time_days is not None else '不明'} 日 / "
                      f"在庫 {'あり' if i.in_stock else ('なし' if i.in_stock is False else '不明')}")
            elif i.candidates:
                flash(f"価格を自動判定できませんでしたが、ページ内に金額が {len(i.candidates)} 件あります。"
                      "部材画面で URL を貼り付けると、どれが価格かを選んで追跡できます。", "error")
            else:
                flash("ページ内に金額が見つかりませんでした (ログインが必要なページの可能性があります)。"
                      f" 調査用に読み取った内容を保存しました: {_save_debug(i)}", "error")
        except pagereader.RateLimited as e:
            flash(f"サイトからアクセス制限を受けました ({e})。しばらく時間を置いてから試してください。", "error")
        except pagereader.FetchError as e:
            flash(f"ページを開けませんでした: {e}", "error")
        return redirect(url_for("sources_page", test_url=url))

    # ================================================================ 発注・入荷
    @app.route("/orders")
    def orders_page():
        tab = request.args.get("tab", "open")
        today = date.today().isoformat()
        where = {"open": f"o.status IN {orders.OPEN}",
                 "late": f"o.status IN {orders.OPEN} AND COALESCE(o.promised_date, o.required_date) < '{today}'",
                 "noeta": "o.status='ordered'",
                 "done": "o.status IN ('received','cancelled')", "all": "1=1"}.get(tab, "1=1")
        rows = orders.listing(conn(), where)
        counts = {k: conn().execute(f"SELECT COUNT(*) FROM orders o WHERE {w}").fetchone()[0] for k, w in {
            "open": f"o.status IN {orders.OPEN}", "noeta": "o.status='ordered'",
            "late": f"o.status IN {orders.OPEN} AND COALESCE(o.promised_date, o.required_date) < '{today}'"}.items()}
        return render_template("orders.html", rows=rows, tab=tab, counts=counts, today=today)

    @app.route("/orders/<int:oid>")
    def order_detail(oid):
        o = orders.get(conn(), oid) or abort(404)
        receipts = conn().execute("SELECT * FROM receipts WHERE order_id=? ORDER BY id", (oid,)).fetchall()
        mails = conn().execute("SELECT DISTINCT e.* FROM emails e JOIN email_materials em ON em.email_id=e.id "
                               "WHERE em.order_id=? OR e.id=? ORDER BY e.id", (oid, o["email_id"] or 0)).fetchall()
        hist = conn().execute("SELECT * FROM activity WHERE material_id=? AND action LIKE 'order%' "
                              "AND created_at >= ? ORDER BY id DESC", (o["material_id"], o["created_at"])).fetchall()
        return render_template("order_detail.html", o=o, receipts=receipts, mails=mails, hist=hist,
                               today=date.today().isoformat())

    @app.post("/materials/<int:mid>/order-record")
    def order_record(mid):
        m = get_or_404("materials", mid)
        f = request.form
        oid = orders.create(conn(), mid, _int(f.get("supplier_id")), _int(f.get("quantity")) or m["quantity"],
                            _float(f.get("unit_price")), f.get("order_date") or None,
                            f.get("required_date") or m["required_date"], note=f.get("note") or None,
                            user_id=uid(), **kw())
        flash("発注を記録しました。納期回答が届いたら「回答納期」を、入荷したら「入荷」を記録してください。")
        return redirect(url_for("order_detail", oid=oid))

    @app.post("/orders/<int:oid>/<action>")
    def order_action(oid, action):
        o = orders.get(conn(), oid) or abort(404)
        f = request.form
        if action == "eta" and f.get("promised_date"):
            orders.set_eta(conn(), oid, f["promised_date"], uid(), **kw(), note=f.get("note") or None)
            flash(f"回答納期 {f['promised_date']} を記録しました。")
        elif action == "receive":
            qty = _int(f.get("quantity")) or (o["quantity"] - o["received_qty"])
            st = orders.receive(conn(), oid, qty, f.get("received_date") or None, f.get("note") or None, uid(), **kw())
            flash("入荷を記録しました。" + (" すべて入荷済みです。" if st == "received" else " 残りは引き続き入荷待ちです。"))
        elif action == "cancel":
            orders.cancel(conn(), oid, f.get("note") or None, uid(), **kw())
            flash("発注をキャンセルにしました。")
        elif action == "inquiry":
            eid = mailflow.create_delivery_inquiry(conn(), o, settings, uid(), **kw())
            if eid:
                return redirect(url_for("email_edit", eid=eid))
            flash("仕入先が未設定のため、納期確認メールを作成できません。", "error")
        return redirect(request.form.get("back") or url_for("order_detail", oid=oid))

    # ================================================================ 受信メール
    @app.route("/inbox")
    def inbox_page():
        tab = request.args.get("tab", "new")
        where = {"new": "i.status='new'", "applied": "i.status='applied'", "ignored": "i.status='ignored'"}.get(tab, "1=1")
        rows = conn().execute(
            "SELECT i.*, e.subject AS matched_subject, e.kind AS matched_kind, s.name AS supplier_name FROM inbox i "
            "LEFT JOIN emails e ON e.id=i.matched_email_id LEFT JOIN suppliers s ON s.id=i.supplier_id "
            f"WHERE {where} ORDER BY COALESCE(i.received_at, i.created_at) DESC LIMIT 300").fetchall()
        counts = {"new": conn().execute("SELECT COUNT(*) FROM inbox WHERE status='new'").fetchone()[0]}
        return render_template("inbox.html", rows=rows, tab=tab, counts=counts,
                               imap_on=inbox_mod.imap_configured(settings))

    @app.post("/inbox/upload")
    def inbox_upload():
        n_new, n_match, bad = 0, 0, []
        for fs in request.files.getlist("files"):
            if not fs or not fs.filename:
                continue
            try:
                p = inbox_mod.parse_file(fs.filename, fs.read())
            except Exception as e:
                bad.append(f"{fs.filename} ({pagereader.short(e, 80)})")
                continue
            iid, new = inbox_mod.store(conn(), p, "upload", actor())
            if new:
                n_new += 1
                n_match += 1 if conn().execute("SELECT matched_email_id FROM inbox WHERE id=?", (iid,)).fetchone()[0] else 0
        log_act("mail_import", None, f"受信メールを {n_new} 件取り込み (対応付け {n_match} 件)")
        flash(f"{n_new} 件取り込みました (送ったメールと対応付けできたもの {n_match} 件)。"
              + (" 読めなかったファイル: " + ", ".join(bad) if bad else ""), "error" if bad else "message")
        return redirect(url_for("inbox_page"))

    @app.post("/inbox/imap")
    def inbox_imap():
        try:
            s = inbox_mod.fetch_imap(conn(), settings, days=_int(request.form.get("days")) or 14, actor=actor())
        except Exception as e:
            flash(f"メールサーバーから取得できませんでした: {pagereader.short(e)}", "error")
            return redirect(url_for("inbox_page"))
        log_act("mail_import", None, f"メールサーバーから {s['imported']} 件取り込み (確認 {s['checked']} 件)")
        flash(f"直近のメール {s['checked']} 件を確認し、仕入先からのメール {s['imported']} 件を取り込みました"
              f" (送ったメールと対応付け {s['matched']} 件)。")
        return redirect(url_for("inbox_page"))

    @app.route("/inbox/<int:iid>")
    def inbox_detail(iid):
        r = get_or_404("inbox", iid)
        ext = json.loads(r["extracted"] or "{}")
        matched = conn().execute("SELECT * FROM emails WHERE id=?", (r["matched_email_id"],)).fetchone() \
            if r["matched_email_id"] else None
        candidates = conn().execute(
            "SELECT e.*, s.name AS supplier_name FROM emails e LEFT JOIN suppliers s ON s.id=e.supplier_id "
            "WHERE e.status='sent' AND e.kind!='alert' ORDER BY (e.supplier_id=?) DESC, e.sent_at DESC LIMIT 40",
            (r["supplier_id"] or 0,)).fetchall()
        return render_template("inbox_detail.html", r=r, ext=ext, matched=matched, candidates=candidates)

    @app.post("/inbox/<int:iid>/<action>")
    def inbox_action(iid, action):
        r = get_or_404("inbox", iid)
        f = request.form
        if action == "match" and _int(f.get("email_id")):
            inbox_mod.rematch(conn(), iid, _int(f.get("email_id")))
            flash("対応するメールを変更しました。抽出結果を確認してください。")
            return redirect(url_for("inbox_detail", iid=iid))
        if action == "ignore":
            conn().execute("UPDATE inbox SET status='ignored', handled_by=?, handled_at=datetime('now','localtime') "
                           "WHERE id=?", (actor(), iid))
            conn().commit()
            log_act("mail_ignore", None, f"受信メール「{(r['subject'] or '')[:40]}」を対応不要にしました")
            return redirect(url_for("inbox_page"))
        if action == "apply":
            ext = json.loads(r["extracted"] or "{}")
            values = []
            for it in ext.get("items", []):
                mid = it["material_id"]
                if not f.get(f"use_{mid}"):
                    continue
                values.append({"material_id": mid, "unit_price": _float(f.get(f"price_{mid}")),
                               "lead_time_days": _int(f.get(f"lead_{mid}")),
                               "promised_date": f.get(f"eta_{mid}") or None,
                               "tax_included": bool(f.get(f"tax_{mid}")), "unchanged": bool(f.get(f"same_{mid}")),
                               "order_id": it.get("order_id")})
            done = inbox_mod.apply(conn(), iid, values, **kw())
            flash(f"{len(done)} 件の部材に反映しました。" if done else "反映する値がありませんでした。対応済みにしました。")
            return redirect(url_for("inbox_page"))
        abort(404)

    # ================================================================ Excel
    def xlsx(data, name):
        from flask import send_file
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/excel")
    def excel_page():
        batches = conn().execute("SELECT id, filename, status, actor, created_at FROM import_batches "
                                 "ORDER BY id DESC LIMIT 10").fetchall()
        return render_template("excel.html", batches=batches)

    @app.route("/excel/template.xlsx")
    def excel_template():
        return xlsx(excel.template(), "部材一括登録テンプレート.xlsx")

    @app.route("/excel/materials.xlsx")
    def excel_export():
        log_act("excel_export", None, "部材一覧を Excel で出力")
        return xlsx(excel.export_materials(conn()), f"部材一覧_{date.today():%Y%m%d}.xlsx")

    @app.post("/excel/upload")
    def excel_upload():
        fs = request.files.get("file")
        if not fs or not fs.filename:
            flash("Excel ファイルを選んでください。", "error")
            return redirect(url_for("excel_page"))
        try:
            preview = excel.parse(conn(), fs.read())
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("excel_page"))
        bid = conn().execute("INSERT INTO import_batches (filename, data, actor) VALUES (?,?,?)",
                             (fs.filename, excel.dumps(preview), actor())).lastrowid
        conn().commit()
        return redirect(url_for("excel_preview", bid=bid))

    @app.route("/excel/preview/<int:bid>")
    def excel_preview(bid):
        b = get_or_404("import_batches", bid)
        pv = json.loads(b["data"])
        staff_names = {r["id"]: r["name"] for r in conn().execute("SELECT id, name FROM staff")}
        sup_names = {r["id"]: r["name"] for r in conn().execute("SELECT id, name FROM suppliers")}
        return render_template("excel_preview.html", b=b, pv=pv, labels=excel.FIELD_LABELS,
                               describe=excel.describe_changes, staff_names=staff_names, sup_names=sup_names)

    @app.post("/excel/apply/<int:bid>")
    def excel_apply(bid):
        b = get_or_404("import_batches", bid)
        if b["status"] != "preview":
            flash("この取り込みは反映済みです。", "error")
            return redirect(url_for("excel_page"))
        counts = excel.apply(conn(), json.loads(b["data"]), uid(), **kw())
        conn().execute("UPDATE import_batches SET status='applied' WHERE id=?", (bid,))
        conn().commit()
        log_act("excel_import", None, f"Excel「{b['filename']}」を反映: 部材 新規 {counts['material_new']} / "
                f"更新 {counts['material_update']} ・ 仕入先 新規 {counts['supplier_new']} / 更新 {counts['supplier_update']}"
                f" ・ 価格 {counts['price']} 件")
        flash(f"反映しました: 部材 新規 {counts['material_new']} 件 / 更新 {counts['material_update']} 件、"
              f"仕入先 新規 {counts['supplier_new']} 件 / 更新 {counts['supplier_update']} 件、価格の記録 {counts['price']} 件")
        return redirect(url_for("materials"))

    @app.route("/charts.xlsx")
    def charts_export():
        period = request.args.get("period", "0")
        since = (datetime.now() - timedelta(days=int(period))).strftime("%Y-%m-%d") if period not in ("0", "") else "0000"
        rows = conn().execute(
            "SELECT m.part_number, m.name, o.observed_at, o.unit_price, o.lead_time_days, o.stock_qty, o.vendor, "
            "o.source, s.name FROM price_observations o JOIN materials m ON m.id=o.material_id "
            "LEFT JOIN suppliers s ON s.id=o.supplier_id WHERE o.observed_at >= ? AND m.active=1 "
            "ORDER BY m.part_number, o.observed_at", (since,)).fetchall()
        data = excel.export_rows("価格推移", ["品番", "品名", "日時", "単価", "納期(日)", "在庫", "販売元", "取得元", "仕入先"],
                                 [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], SOURCE_LABELS.get(r[7], r[7]), r[8]) for r in rows])
        return xlsx(data, f"価格推移_{date.today():%Y%m%d}.xlsx")

    # ================================================================ 履歴
    def history_query():
        a = request.args
        where, args = ["1=1"], []
        if a.get("who"):
            where.append("COALESCE(h.actor, s.name) = ?")
            args.append(a["who"])
        if a.get("action"):
            where.append("h.action LIKE ?")
            args.append(a["action"] + "%")
        if a.get("q"):
            where.append("(m.part_number LIKE ? OR m.name LIKE ? OR h.detail LIKE ?)")
            args += [f"%{a['q']}%"] * 3
        if a.get("from"):
            where.append("h.created_at >= ?")
            args.append(a["from"])
        if a.get("to"):
            where.append("h.created_at < date(?, '+1 day')")
            args.append(a["to"])
        return (" AND ".join(where), args)

    HISTORY_SQL = ("SELECT h.*, COALESCE(h.actor, s.name, '自動') AS who, m.part_number, m.name AS material_name "
                   "FROM activity h LEFT JOIN staff s ON s.id=h.user_id LEFT JOIN materials m ON m.id=h.material_id ")

    @app.route("/history")
    def history():
        where, args = history_query()
        page = max(1, _int(request.args.get("page")) or 1)
        rows = conn().execute(HISTORY_SQL + f"WHERE {where} ORDER BY h.id DESC LIMIT 101 OFFSET ?",
                              args + [(page - 1) * 100]).fetchall()
        people = [r[0] for r in conn().execute("SELECT DISTINCT COALESCE(h.actor, s.name) FROM activity h "
                                               "LEFT JOIN staff s ON s.id=h.user_id WHERE COALESCE(h.actor, s.name) "
                                               "IS NOT NULL ORDER BY 1")]
        groups = {"material": "部材", "order": "発注・入荷", "email": "メール", "fetch": "価格取得",
                  "observation": "価格の記録", "mail": "受信メール", "excel": "Excel", "comment": "コメント",
                  "settings": "設定", "supplier": "仕入先", "staff": "担当者"}
        return render_template("history.html", rows=rows[:100], more=len(rows) > 100, page=page, people=people,
                               groups=groups)

    @app.route("/history.xlsx")
    def history_export():
        where, args = history_query()
        rows = conn().execute(HISTORY_SQL + f"WHERE {where} ORDER BY h.id DESC LIMIT 20000", args).fetchall()
        data = excel.export_rows("操作履歴", ["日時", "操作者", "操作", "品番", "品名", "内容", "端末"],
                                 [(r["created_at"], r["who"], ACTION_LABELS.get(r["action"], r["action"]),
                                   r["part_number"], r["material_name"], r["detail"], r["client"]) for r in rows])
        return xlsx(data, f"操作履歴_{date.today():%Y%m%d}.xlsx")

    # ================================================================ リマインドの定期スキャン
    if settings.background:
        def loop():
            time.sleep(10)
            while True:
                try:
                    c = db.connect(settings.database)
                    try:
                        s = reminders.scan(c, settings)
                        if any(s.values()):
                            log.info("リマインド: %s", s)
                    finally:
                        c.close()
                except Exception:
                    log.exception("reminder scan failed")
                time.sleep(settings.reminder_interval_minutes * 60)
        threading.Thread(target=loop, daemon=True, name="reminders").start()

    return app
