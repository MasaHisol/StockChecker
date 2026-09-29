import json
from datetime import date

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import db, free_sources, online, pagereader, service
from .config import Settings
from .providers import DemoProvider, HttpJsonProvider, parse_price_csv


def build_providers(settings, demo=False):
    """設定済みの取得元をすべて返す。キーが未設定のものは使わない。"""
    ps = []
    if settings.price_feed_url:
        ps.append(HttpJsonProvider(settings.price_feed_url))
    if settings.page_watch:
        ps.append(free_sources.PageWatchProvider(settings.database))
    if settings.yahoo_app_id:
        ps.append(free_sources.YahooShoppingProvider(settings.yahoo_app_id))
    if settings.rakuten_app_id:
        ps.append(free_sources.RakutenProvider(settings.rakuten_app_id))
    if settings.mouser_api_key:
        ps.append(online.MouserProvider(settings.mouser_api_key))
    if settings.digikey_client_id and settings.digikey_client_secret:
        ps.append(online.DigiKeyProvider(settings.digikey_client_id, settings.digikey_client_secret))
    if settings.anthropic_api_key and settings.web_search != "off":
        try:
            web = online.WebSearchProvider(settings.anthropic_api_key)
            web.fallback_only = True
            ps.append(web)
        except ImportError:
            pass
    if demo:
        ps.append(DemoProvider())
    return ps


def source_status(settings):
    return [("商品ページ監視", settings.page_watch),
            ("Yahoo!ショッピング", bool(settings.yahoo_app_id)),
            ("楽天市場", bool(settings.rakuten_app_id)),
            ("Mouser API", bool(settings.mouser_api_key)),
            ("Digi-Key API", bool(settings.digikey_client_id and settings.digikey_client_secret)),
            (f"Web 検索 (Claude, {settings.web_search})",
             bool(settings.anthropic_api_key) and settings.web_search != "off"),
            ("価格フィード URL", bool(settings.price_feed_url))]


def _int(v):
    return int(v) if v not in (None, "") else None


def _float(v):
    return float(v.replace(",", "")) if v not in (None, "") else None


def create_app(settings=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "stockchecker-local"
    app.config["SC"] = settings
    app.jinja_env.filters["fromjson"] = json.loads

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

    app.jinja_env.filters["yen"] = lambda v: f"{v:,.2f}".rstrip("0").rstrip(".") if v < 100 else f"{v:,.0f}"

    KIND_LABELS = {"rfq": "見積依頼", "order": "注文", "alert": "担当者通知"}
    SOURCE_LABELS = {"manual": "手入力", "quote": "見積回答", "csv": "CSV 取込", "demo": "デモ",
                     "page": "ページ監視", "yahoo": "Yahoo!", "rakuten": "楽天", "mouser": "Mouser",
                     "digikey": "Digi-Key", "web": "Web 検索 (AI)", "http": "価格フィード"}

    def status_pill(s):
        from markupsafe import Markup
        cls, label = {"draft": ("warn", "下書き"), "sent": ("good", "送信済"),
                      "failed": ("crit", "送信失敗")}[s]
        return Markup(f'<span class="pill {cls}">{label}</span>')

    @app.context_processor
    def _ctx():
        c = conn()
        drafts = c.execute("SELECT COUNT(*) FROM emails WHERE status='draft'").fetchone()[0]
        quote = c.execute("SELECT COUNT(DISTINCT a.material_id) FROM alerts a JOIN materials m "
                          "ON m.id=a.material_id WHERE a.status='open' AND m.active=1").fetchone()[0]
        return {"settings": settings, "sources": source_status(settings),
                "nav_counts": {"drafts": drafts, "quote": quote}, "kind_labels": KIND_LABELS,
                "source_labels": SOURCE_LABELS, "status_pill": status_pill}

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
            rows.append({"m": m, "latest": latest, "findings": findings, "needs_quote": nq,
                         "status": "quote" if nq else ("warn" if findings else "ok"),
                         "delta": delta, "days_left": days_left})
        order = {"quote": 0, "warn": 1, "ok": 2}
        rows.sort(key=lambda r: order[r["status"]])
        alerts = c.execute(
            "SELECT a.*, m.name, m.part_number FROM alerts a JOIN materials m ON m.id=a.material_id "
            "WHERE a.status='open' ORDER BY a.created_at DESC LIMIT 50").fetchall()
        drafts = c.execute("SELECT COUNT(*) FROM emails WHERE status='draft'").fetchone()[0]
        last = c.execute("SELECT MAX(observed_at) FROM price_observations WHERE source NOT IN "
                         "('manual','quote','csv')").fetchone()[0]
        return render_template("index.html", rows=rows, alerts=alerts, drafts=drafts,
                               last_fetch=last[:16] if last else None)

    @app.post("/run-checks")
    def run_checks():
        demo = request.form.get("demo") == "1"
        s = service.run_checks(conn(), settings, build_providers(settings, demo))
        flash(f"チェック完了: 部材 {s['materials']} 件 / 価格取得 {s['observations']} 件 / "
              f"新規アラート {s['new_alerts']} 件 / 見積依頼下書き {s['rfq_drafts']} 件 "
              f"(自動送信 {s['rfq_sent']} 件) / 担当者通知 {s['notifications']} 件")
        for err in s["errors"][:5]:
            flash(f"取得エラー: {err}", "error")
        return redirect(url_for("index"))

    # ---- 部材 ------------------------------------------------------------
    def material_form_values():
        f = request.form
        return (f["part_number"].strip(), f["name"].strip(), f.get("maker") or None,
                f.get("spec") or None, f.get("unit") or "個", _int(f.get("quantity")) or 1,
                f.get("required_date") or None, _float(f.get("budget_unit_price")),
                1 if f.get("custom_item") else 0, _int(f.get("owner_id")),
                _int(f.get("preferred_supplier_id")), f.get("notes") or None,
                (f.get("watch_urls") or "").strip() or None)

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
                    "preferred_supplier_id, notes, watch_urls) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    material_form_values())
                conn().commit()
            except Exception as e:
                flash(f"登録できませんでした: {e}")
                return render_template("material_form.html", m=request.form, **masters())
            price = _float(request.form.get("init_price"))
            wurl = (request.form.get("watch_urls") or "").strip().splitlines()
            lt = _int(request.form.get("init_lead_time"))
            if price is not None or lt is not None:
                src = "page" if wurl else "manual"
                db.add_observation(conn(), cur.lastrowid, src, price, lt,
                                   vendor=pagereader.domain(wurl[0]) if wurl else None,
                                   url=wurl[0].strip() if wurl else None)
            return redirect(url_for("material_detail", mid=cur.lastrowid))
        prefill = {}
        url = request.args.get("url", "").strip()
        if url:
            try:
                info = pagereader.read(url, _hint_for(url))
                prefill = {"name": info.title, "part_number": info.part_number, "maker": info.maker,
                           "watch_urls": url, "init_price": pagereader.net_price(info),
                           "init_lead_time": info.lead_time_days}
                flash("商品ページから入力しました。品番・品名を確認して保存してください。"
                      + ("" if info.unit_price is not None else
                         " 価格は自動で読み取れなかったため、保存後に部材画面で価格の場所を指定してください。"))
            except pagereader.FetchError as e:
                flash(f"ページを開けませんでした: {e}", "error")
                prefill = {"watch_urls": url}
        return render_template("material_form.html", m=prefill, **masters())

    @app.route("/materials/<int:mid>/edit", methods=["GET", "POST"])
    def material_edit(mid):
        m = get_or_404("materials", mid)
        if request.method == "POST":
            conn().execute(
                "UPDATE materials SET part_number=?, name=?, maker=?, spec=?, unit=?, quantity=?, "
                "required_date=?, budget_unit_price=?, custom_item=?, owner_id=?, "
                "preferred_supplier_id=?, notes=?, watch_urls=? WHERE id=?", material_form_values() + (mid,))
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
        chart = [{"t": o["observed_at"][:10], "p": o["unit_price"], "v": o["vendor"] or ""}
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

    @app.post("/materials/<int:mid>/fetch")
    def fetch_now(mid):
        m = get_or_404("materials", mid)
        ps = build_providers(settings)
        for p in ps:
            p.fallback_only = False  # 手動取得では Web 検索も含め全取得元を使う
        if not ps:
            flash("ネット取得元が未設定です (stockchecker.ini の [sources] を設定してください)。")
            return redirect(url_for("material_detail", mid=mid))
        errors = []
        n = service.refresh_prices(conn(), ps, [m], None, errors)
        flash("最新の価格・納期を取得しました。" if n else "価格情報が見つかりませんでした。")
        for err in errors:
            flash(f"取得エラー: {err}", "error")
        return redirect(url_for("material_detail", mid=mid))

    def _add_watch_url(mid, url):
        m = get_or_404("materials", mid)
        urls = [u.strip() for u in (m["watch_urls"] or "").splitlines() if u.strip()]
        if url not in urls:
            urls.append(url)
        conn().execute("UPDATE materials SET watch_urls=? WHERE id=?", ("\n".join(urls), mid))
        conn().commit()

    def _record_page(m, info, method):
        offer = free_sources.info_to_offer(info, m["quantity"] or 1)
        offer["note"] = method
        db.add_observation(conn(), m["id"], "page", unit_price=offer["unit_price"],
                           lead_time_days=offer["lead_time_days"], stock_qty=offer["stock_qty"],
                           vendor=offer["vendor"], url=offer["url"],
                           detail=json.dumps([offer], ensure_ascii=False))
        return offer

    def _save_debug(info):
        """読み取れなかったページの HTML を保存し、パスを返す (問い合わせ用)。"""
        if not info or not getattr(info, "html", None):
            return None
        from datetime import datetime
        from pathlib import Path
        d = Path(settings.database).resolve().parent / "debug"
        d.mkdir(exist_ok=True)
        path = d / f"{pagereader.domain(info.url).replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.html"
        path.write_text(info.html, encoding="utf-8")
        return str(path)

    def _hint_for(url):
        r = conn().execute("SELECT * FROM site_hints WHERE domain=?",
                           (pagereader.domain(url),)).fetchone()
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
        if info.unit_price is None:
            return render_template("watch_pick.html", m=m, info=info, url=url,
                                   debug_path=_save_debug(info))
        _add_watch_url(mid, url)
        o = _record_page(m, info, info.method)
        flash(f"追跡を開始しました: 単価 ¥{o['unit_price']:,.0f}"
              f"{' (税込から換算)' if info.tax_included else ''} / 読み取り方法: {info.method}。"
              "違う金額の場合は「価格の場所を指定し直す」を押してください。")
        return redirect(url_for("material_detail", mid=mid, repick=url))

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
            c = json.loads(f["choice"])
            info.unit_price, info.tax_included = c["value"], f.get("tax") == "incl"
            if c.get("label"):
                conn().execute("INSERT OR REPLACE INTO site_hints (domain, label, tax) VALUES (?,?,?)",
                               (pagereader.domain(url), c["label"], f.get("tax") or None))
                conn().commit()
            method = f"見出し「{c.get('label') or '—'}」"
        _add_watch_url(mid, url)
        o = _record_page(m, info, method)
        flash(f"追跡を開始しました: 単価 ¥{o['unit_price']:,.0f}。"
              + ("次回からこのサイトは同じ見出しの金額を自動で読み取ります。" if manual is None else ""))
        return redirect(url_for("material_detail", mid=mid))

    @app.post("/materials/<int:mid>/watch/remove")
    def watch_remove(mid):
        m = get_or_404("materials", mid)
        url = request.form.get("url", "")
        urls = [u.strip() for u in (m["watch_urls"] or "").splitlines() if u.strip() and u.strip() != url]
        conn().execute("UPDATE materials SET watch_urls=? WHERE id=?", ("\n".join(urls) or None, mid))
        conn().commit()
        flash("監視ページを外しました。")
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

    @app.route("/sources")
    def sources_page():
        s = settings
        info = [
            dict(name="商品ページ監視", free=True, on=s.page_watch,
                 desc="部材ごとに登録した通販サイトの商品ページを巡回し、ページに埋め込まれた価格・在庫・納期を読み取ります。",
                 fit="モノタロウ・ミスミ・アスクル・メーカー直販など、商品ページが決まっている部材",
                 how="部材の編集画面で URL を登録 (page_watch = 1)"),
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
                 desc="AI が Web を検索して相場を調べます。他の取得元で見つからない部材の最後の手段です。",
                 fit="何でも (鋼材・機構部品・特殊品など)",
                 how="anthropic_api_key と web_search = fallback"),
        ]
        return render_template("sources.html", source_info=info,
                               test=request.args.get("test_url"))

    @app.post("/sources/test")
    def sources_test():
        url = request.form.get("url", "").strip()
        try:
            i = pagereader.read(url, _hint_for(url))
            if i.unit_price is not None:
                flash(f"読み取り成功: 単価 ¥{i.unit_price:,.0f} ({i.method}{' / ブラウザ表示' if i.rendered else ''}) / "
                      f"納期 {i.lead_time_days if i.lead_time_days is not None else '不明'} 日 / "
                      f"在庫 {'あり' if i.in_stock else ('なし' if i.in_stock is False else '不明')}")
            elif i.candidates:
                flash(f"価格を自動判定できませんでしたが、ページ内に金額が {len(i.candidates)} 件あります。"
                      "部材画面で URL を貼り付けると、どれが価格かを選んで追跡できます。", "error")
            else:
                flash("ページ内に金額が見つかりませんでした (ログインが必要なページの可能性があります)。"
                      f" 調査用に読み取った内容を保存しました: {_save_debug(i)}", "error")
        except pagereader.FetchError as e:
            flash(f"ページを開けませんでした: {e}", "error")
        return redirect(url_for("sources_page", test_url=url))

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
