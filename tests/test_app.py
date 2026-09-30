import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stockchecker import create_app, db, mailflow, reminders, service
from stockchecker.config import Settings


def make(tmp_path, auto=False, **kw):
    s = Settings(database=str(tmp_path / "t.db"), outbox_dir=str(tmp_path / "out"),
                 auto_send_enabled=auto, csrf=False, run_jobs_async=False,
                 page_watch_render=False, **kw)
    app = create_app(s)
    conn = db.connect(s.database)
    conn.execute("INSERT INTO staff (name,email) VALUES ('担当','o@example.com')")
    conn.execute("INSERT INTO suppliers (name,email,auto_send_rfq) VALUES ('仕入先','s@example.com',1)")
    conn.execute("INSERT INTO materials (part_number,name,quantity,owner_id,preferred_supplier_id) "
                 "VALUES ('P-1','部品',5,1,1)")
    conn.commit()
    return app, s, conn


def test_run_checks_creates_alert_and_rfq_draft(tmp_path):
    _, s, conn = make(tmp_path)
    r = service.run_checks(conn, s)
    assert r["new_alerts"] == 1 and r["rfq_drafts"] == 1 and r["rfq_sent"] == 0
    assert r["notifications"] == 1
    r = service.run_checks(conn, s)  # 2 回目は重複しない
    assert r["new_alerts"] == 0 and r["rfq_drafts"] == 0
    db.add_observation(conn, 1, "manual", 100, 3)
    service.run_checks(conn, s)
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0] == 0


def test_auto_send(tmp_path):
    _, s, conn = make(tmp_path, auto=True)
    assert service.run_checks(conn, s)["rfq_sent"] == 1
    assert len(list((tmp_path / "out").glob("*.eml"))) == 2  # RFQ + 担当者通知


def test_pages(tmp_path):
    app, s, conn = make(tmp_path)
    db.add_observation(conn, 1, "manual", 100, 3, observed_at="2026-06-01 09:00:00")
    db.add_observation(conn, 1, "quote", 110, 5)
    c = app.test_client()
    r = c.post("/materials/1/mail/rfq", data={})
    assert r.status_code == 302
    for url in ["/", "/?mine=1", "/materials", "/materials/1", "/charts", "/charts?period=0&q=P",
                "/emails", "/emails?tab=waiting", "/emails?tab=all", "/masters", "/import",
                "/materials/new", "/materials/1/edit", "/settings", "/settings/templates?kind=confirm",
                "/sources", "/orders", "/orders?tab=late", "/orders?tab=all", "/inbox", "/inbox?tab=all",
                "/excel", "/history", "/history?who=担当&action=email", r.headers["Location"]]:
        assert c.get(url).status_code == 200, url
    assert "chartpts" not in c.get("/materials/1").get_data(as_text=True)
    r = c.post("/materials/1/mail/order", data={"supplier_id": 1, "quantity": 5, "unit_price": "100"})
    assert r.status_code == 302
    r = c.post("/import", data={"text": "part_number,unit_price,lead_time_days\nP-1,120,4\nX,1,1"})
    assert r.status_code == 302
    r = c.post("/emails/1", data={"to_addr": "s@example.com", "subject": "件名", "body": "本文", "action": "send"})
    assert r.status_code == 302
    assert conn.execute("SELECT status FROM emails WHERE id=1").fetchone()[0] == "sent"
    c.post("/materials/1/comment", data={"body": "電話済み"})
    assert "電話済み" in c.get("/materials/1").get_data(as_text=True)


def test_operator_name_and_csrf_and_history(tmp_path):
    s = Settings(database=str(tmp_path / "t.db"), outbox_dir=str(tmp_path / "o"))
    app = create_app(s)
    c = app.test_client()
    page = c.get("/").get_data(as_text=True)
    assert "あなたの名前を選んでください" in page  # ログインは無く、名前だけ選ぶ
    assert c.post("/materials/new", data={"part_number": "X", "name": "x"}).status_code == 400  # CSRF
    c.post("/operator", data={"name": "佐藤", "_csrf": _token(c)})
    assert "あなたの名前を選んでください" not in c.get("/").get_data(as_text=True)
    c.post("/materials/new", data={"part_number": "X", "name": "x", "quantity": "1", "_csrf": _token(c)})
    c.post("/materials/1/edit", data={"part_number": "X", "name": "xx", "quantity": "3", "_csrf": _token(c)})
    conn = db.connect(s.database)
    acts = conn.execute("SELECT * FROM activity ORDER BY id").fetchall()
    assert [a["actor"] for a in acts] == ["佐藤", "佐藤"]
    assert "品名: x → xx" in acts[1]["detail"] and "数量: 1 → 3" in acts[1]["detail"]
    h = c.get("/history?who=佐藤").get_data(as_text=True)
    assert "品名: x → xx" in h
    r = c.get("/history.xlsx?who=佐藤")
    assert r.status_code == 200 and r.data[:2] == b"PK"


def _token(client):
    with client.session_transaction() as sess:
        return sess.get("csrf")


class _Shop(BaseHTTPRequestHandler):
    hits = {}

    def do_GET(self):
        n = _Shop.hits[self.path] = _Shop.hits.get(self.path, 0) + 1
        if self.path == "/limited" and n == 1:  # 初回だけ制限
            self.send_response(429)
            self.end_headers()
            return
        price = {"/a": 1200, "/limited": 800}.get(self.path)
        if price is None:
            body = "<html><title>x</title><body>no price</body></html>"
        else:
            body = ('<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer",'
                    f'"price":"{price}","availability":"https://schema.org/InStock"}}}}</script>'
                    "<div>通常 2営業日 出荷</div>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def shop(monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    srv = HTTPServer(("127.0.0.1", 0), _Shop)
    _Shop.hits = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_bulk_fetch_job_retries_rate_limit(tmp_path, shop):
    app, s, conn = make(tmp_path)
    for k, v in {"fetch_default_delay": "0", "fetch_backoff_seconds": "0", "fetch_domain_delays": ""}.items():
        db.set_setting(conn, k, v)
    conn.execute("UPDATE materials SET watch_urls=? WHERE id=1", (f"{shop}/a",))
    conn.execute("INSERT INTO materials (part_number,name,watch_urls) VALUES ('P-2','制限あり',?)", (f"{shop}/limited",))
    conn.execute("INSERT INTO materials (part_number,name,watch_urls) VALUES ('P-3','価格なし',?)", (f"{shop}/none",))
    conn.execute("INSERT INTO materials (part_number,name) VALUES ('P-4','URLなし')")
    conn.commit()
    c = app.test_client()
    r = c.post("/fetch", data={})
    assert r.status_code == 302
    job = conn.execute("SELECT * FROM fetch_jobs ORDER BY id DESC").fetchone()
    assert job["status"] == "done" and (job["ok"], job["failed"], job["skipped"]) == (2, 1, 1)
    assert _Shop.hits["/limited"] == 2  # 429 の後に再試行して成功
    assert db.latest_observation(conn, 2)["unit_price"] == 800
    assert db.latest_observation(conn, 1)["lead_time_days"] == 3
    assert c.get(f"/fetch/{job['id']}").status_code == 200
    assert json.loads(c.get(f"/api/fetch/{job['id']}").data)["done"] == 4
    # 失敗・対象外は理由が記録される
    msgs = {r["material_id"]: r["message"] for r in conn.execute("SELECT * FROM fetch_job_items")}
    assert "読み取れません" in msgs[3] and "URL が未登録" in msgs[4]


def test_one_touch_mail_reminder_and_answer(tmp_path):
    app, s, conn = make(tmp_path)
    c = app.test_client()
    eid = int(c.post("/materials/1/mail/confirm", data={}).headers["Location"].rsplit("/", 1)[1])
    e = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert e["kind"] == "confirm" and "価格・納期ご確認" in e["subject"] and "部品" in e["body"]
    assert e["cc_addr"] == "o@example.com"
    c.post(f"/emails/{eid}", data={"to_addr": e["to_addr"], "subject": e["subject"], "body": e["body"], "action": "send"})
    # 6 日後: 回答が無いので督促のリマインド
    later = datetime.now() + timedelta(days=6)
    summary = reminders.scan(conn, s, now=later)
    assert summary["no_reply"] == 1 and summary["notifications"] == 1
    assert reminders.scan(conn, s, now=later)["no_reply"] == 0  # 重複しない
    home = c.get("/").get_data(as_text=True)
    assert "督促メールを作成" in home
    fid = int(c.post("/materials/1/mail/followup", data={"email_id": eid}).headers["Location"].rsplit("/", 1)[1])
    f = conn.execute("SELECT * FROM emails WHERE id=?", (fid,)).fetchone()
    assert f["kind"] == "followup" and f["followup_of"] == eid and e["subject"] in f["subject"]
    # 見積回答を記録 → 回答済み・アラート解消・確認日更新
    c.post("/materials/1/observations", data={"unit_price": "150", "lead_time_days": "7", "source": "quote"})
    assert conn.execute("SELECT answered_at FROM emails WHERE id=?", (eid,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='no_reply' AND status='open'").fetchone()[0] == 0
    assert conn.execute("SELECT last_confirmed_at FROM materials WHERE id=1").fetchone()[0]


def test_confirm_cycle_reminder_and_auto_send(tmp_path, monkeypatch):
    _, s, conn = make(tmp_path)
    conn.execute("UPDATE materials SET created_at='2026-01-01 00:00:00'")
    conn.commit()
    now = datetime(2026, 9, 30)
    assert reminders.scan(conn, s, now=now)["confirm_due"] == 1  # 既定 90 日を過ぎた
    a = conn.execute("SELECT * FROM alerts WHERE kind='confirm_due'").fetchone()
    assert "確認時期" in a["message"]
    # 自動送信: 管理者設定 + 部材設定 + SMTP がそろうと確認メールを送る
    sent = []
    monkeypatch.setattr("stockchecker.mailer.deliver", lambda msg, st: sent.append(msg) or "smtp://test")
    s.smtp_host = "smtp.example.com"
    db.set_setting(conn, "auto_confirm_enabled", "1")
    conn.execute("UPDATE materials SET auto_confirm=1")
    conn.execute("UPDATE alerts SET status='resolved'")
    conn.commit()
    assert reminders.scan(conn, s, now=now)["auto_confirm"] == 1
    e = conn.execute("SELECT * FROM emails WHERE kind='confirm'").fetchone()
    assert e["status"] == "sent" and e["auto"] == 1 and sent
    assert reminders.scan(conn, s, now=now)["auto_confirm"] == 0  # 回答待ちの間は再送しない
    # URL で追跡している部材は既定では確認対象外
    conn.execute("UPDATE materials SET watch_urls='https://example.com/p' WHERE id=1")
    cfg = mailflow.reminder_config(conn)
    assert reminders.effective_interval(conn.execute("SELECT * FROM materials").fetchone(), cfg) == 0


def test_bulk_mail_groups_by_supplier(tmp_path):
    app, s, conn = make(tmp_path)
    conn.execute("INSERT INTO suppliers (name,email) VALUES ('別の仕入先','b@example.com')")
    conn.execute("INSERT INTO materials (part_number,name,preferred_supplier_id) VALUES ('P-2','部品2',1)")
    conn.execute("INSERT INTO materials (part_number,name,preferred_supplier_id) VALUES ('P-3','部品3',2)")
    conn.execute("INSERT INTO materials (part_number,name) VALUES ('P-4','仕入先なし')")
    conn.commit()
    c = app.test_client()
    r = c.post("/materials/bulk", data={"action": "rfq", "ids": ["1", "2", "3", "4"]}, follow_redirects=True)
    assert "P-4" in r.get_data(as_text=True)  # 仕入先未設定の警告
    mails = conn.execute("SELECT * FROM emails ORDER BY id").fetchall()
    assert len(mails) == 2
    first = mails[0]
    assert "他 1 件" in first["subject"] and "P-1" in first["body"] and "P-2" in first["body"]
    assert len(mailflow.linked_materials(conn, first["id"])) == 2


def test_template_edit_applies(tmp_path):
    app, s, conn = make(tmp_path)
    c = app.test_client()
    c.post("/settings/templates?kind=rfq", data={"subject": "見積お願い {title}", "body": "{contact_name} 様\n{items}"})
    eid = int(c.post("/materials/1/mail/rfq", data={}).headers["Location"].rsplit("/", 1)[1])
    e = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert e["subject"] == "見積お願い 部品 (P-1)" and e["body"].startswith("ご担当者 様")


def test_order_flow_from_email_to_receipt(tmp_path):
    app, s, conn = make(tmp_path)
    c = app.test_client()
    c.post("/operator", data={"name": "購買A"})
    eid = int(c.post("/materials/1/mail/order", data={"quantity": "5", "unit_price": "120",
                                                      "delivery_date": "2026-10-10"}).headers["Location"].rsplit("/", 1)[1])
    e = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    c.post(f"/emails/{eid}", data={"to_addr": e["to_addr"], "subject": e["subject"], "body": e["body"], "action": "send"})
    o = conn.execute("SELECT * FROM orders").fetchone()
    assert (o["quantity"], o["unit_price"], o["required_date"], o["status"]) == (5, 120, "2026-10-10", "ordered")
    assert conn.execute("SELECT message_id FROM emails WHERE id=?", (eid,)).fetchone()[0]
    # 希望納期を過ぎても未入荷 → 入荷遅れのリマインド
    s2 = reminders.scan(conn, s, now=datetime(2026, 10, 12))
    assert s2["delivery_late"] == 1
    assert "入荷遅れ" in c.get("/").get_data(as_text=True)
    # 納期確認メール → 回答納期 → 分納 → 完納
    c.post(f"/orders/{o['id']}/inquiry")
    d = conn.execute("SELECT * FROM emails WHERE kind='delivery'").fetchone()
    assert d and "納期ご確認" in d["subject"] and str(o["order_date"]) in d["body"]
    c.post(f"/orders/{o['id']}/eta", data={"promised_date": "2026-10-20"})
    assert conn.execute("SELECT status, promised_date FROM orders").fetchone()[:] == ("confirmed", "2026-10-20")
    reminders.scan(conn, s, now=datetime(2026, 10, 12))
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='delivery_late' AND status='open'").fetchone()[0] == 0
    c.post(f"/orders/{o['id']}/receive", data={"quantity": "2"})
    assert conn.execute("SELECT status, received_qty FROM orders").fetchone()[:] == ("partial", 2)
    c.post(f"/orders/{o['id']}/receive", data={})  # 残り全部
    assert conn.execute("SELECT status, received_qty FROM orders").fetchone()[:] == ("received", 5)
    acts = [r["action"] for r in conn.execute("SELECT action FROM activity WHERE actor='購買A'")]
    assert {"order_new", "order_eta", "order_receive"} <= set(acts)
    assert c.get(f"/orders/{o['id']}").status_code == 200
    # 発注の単価は価格の履歴にも残る
    assert conn.execute("SELECT COUNT(*) FROM price_observations WHERE source='order'").fetchone()[0] == 1


def test_manual_order_and_no_eta_reminder(tmp_path):
    app, s, conn = make(tmp_path)
    c = app.test_client()
    c.post("/materials/1/order-record", data={"supplier_id": "1", "quantity": "3", "order_date": "2026-09-01"})
    assert reminders.scan(conn, s, now=datetime(2026, 9, 30))["order_no_eta"] == 1


def _reply_eml(msgid_ref, subject, body, frm="s@example.com"):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = f"仕入先 <{frm}>"
    m["To"] = "purchasing@example.com"
    m["Subject"] = subject
    m["Message-ID"] = "<reply-1@example.com>"
    if msgid_ref:
        m["In-Reply-To"] = msgid_ref
    m["Date"] = "Wed, 30 Sep 2026 10:00:00 +0900"
    m.set_content(body)
    return bytes(m)


def test_inbox_upload_match_extract_apply(tmp_path):
    import io
    app, s, conn = make(tmp_path)
    c = app.test_client()
    eid = int(c.post("/materials/1/mail/rfq", data={}).headers["Location"].rsplit("/", 1)[1])
    e = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    c.post(f"/emails/{eid}", data={"to_addr": e["to_addr"], "subject": e["subject"], "body": e["body"], "action": "send"})
    mid = conn.execute("SELECT message_id FROM emails WHERE id=?", (eid,)).fetchone()[0]
    raw = _reply_eml(mid, "Re: " + e["subject"], "お世話になっております。\nP-1 部品: 単価 ¥1,250 (税抜)、納期 受注後2週間です。")
    c.post("/inbox/upload", data={"files": (io.BytesIO(raw), "reply.eml")}, content_type="multipart/form-data")
    r = conn.execute("SELECT * FROM inbox").fetchone()
    assert r["matched_email_id"] == eid and "In-Reply-To" in r["match_method"]
    ext = json.loads(r["extracted"])["items"][0]
    assert ext["unit_price"] == 1250 and ext["lead_time_days"] == 14
    assert conn.execute("SELECT answered_at FROM emails WHERE id=?", (eid,)).fetchone()[0]  # 返信 = 回答あり
    assert "単価" in c.get(f"/inbox/{r['id']}").get_data(as_text=True)
    c.post(f"/inbox/{r['id']}/apply", data={"use_1": "1", "price_1": "1250", "lead_1": "14"})
    last = db.latest_observation(conn, 1)
    assert (last["source"], last["unit_price"], last["lead_time_days"]) == ("quote", 1250, 14)
    assert conn.execute("SELECT status FROM inbox").fetchone()[0] == "applied"
    # 同じメールをもう一度取り込んでも重複しない
    c.post("/inbox/upload", data={"files": (io.BytesIO(raw), "reply.eml")}, content_type="multipart/form-data")
    assert conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1


def test_inbox_imap_only_keeps_supplier_mail(tmp_path):
    from stockchecker import inbox
    _, s, conn = make(tmp_path)
    s.imap_host, s.imap_user = "imap.example.com", "u"
    msgs = [_reply_eml(None, "見積の件", "P-1 単価 900円").replace(b"reply-1", b"a1"),
            _reply_eml(None, "ニュースレター", "セール", frm="news@shop.example").replace(b"reply-1", b"a2")]

    class FakeIMAP:
        def __init__(self, host, port):
            pass

        def login(self, u, p):
            pass

        def select(self, folder, readonly):
            assert readonly

        def search(self, *a):
            return "OK", [b"1 2"]

        def fetch(self, num, what):
            raw = msgs[int(num) - 1]
            return "OK", [(b"x", raw)]

        def logout(self):
            pass
    summary = inbox.fetch_imap(conn, s, client_factory=FakeIMAP)
    assert summary["imported"] == 1 and conn.execute("SELECT from_addr FROM inbox").fetchone()[0] == "s@example.com"


def test_excel_template_import_preview_apply(tmp_path):
    import io
    from openpyxl import load_workbook
    from stockchecker import excel
    app, s, conn = make(tmp_path)
    c = app.test_client()
    tpl = c.get("/excel/template.xlsx")
    assert tpl.status_code == 200
    wb = load_workbook(io.BytesIO(tpl.data))
    ws, ws2 = wb["部材"], wb["仕入先"]
    ws.delete_rows(2)
    ws.append(["P-1", "部品 (更新)", None, None, 10])                                    # 既存を更新
    ws.append(["N-1", "新しい部材", "ミスミ", None, 4, "本", "2026/12/01", 500, "はい", "担当",
               "新仕入先", "https://example.com/p/1", 60, "いいえ", None, 480, 7])          # 新規 (新しい仕入先)
    ws.append(["N-2", None])                                                               # エラー
    ws2.delete_rows(2)
    ws2.append(["新仕入先", "田中", "new@example.com"])
    buf = io.BytesIO()
    wb.save(buf)
    r = c.post("/excel/upload", data={"file": (io.BytesIO(buf.getvalue()), "parts.xlsx")},
               content_type="multipart/form-data")
    bid = int(r.headers["Location"].rsplit("/", 1)[1])
    page = c.get(f"/excel/preview/{bid}").get_data(as_text=True)
    assert "品名: 部品 → 部品 (更新)" in page and "品番と品名は必須です" in page
    c.post(f"/excel/apply/{bid}")
    n = conn.execute("SELECT * FROM materials WHERE part_number='N-1'").fetchone()
    sup = conn.execute("SELECT * FROM suppliers WHERE name='新仕入先'").fetchone()
    assert n["preferred_supplier_id"] == sup["id"] and n["custom_item"] == 1 and n["required_date"] == "2026-12-01"
    assert n["owner_id"] == 1 and n["confirm_interval_days"] == 60
    assert conn.execute("SELECT name, quantity FROM materials WHERE part_number='P-1'").fetchone()[:] == ("部品 (更新)", 10)
    assert db.latest_observation(conn, n["id"])["unit_price"] == 480
    assert conn.execute("SELECT COUNT(*) FROM materials WHERE part_number='N-2'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity WHERE action='excel_import'").fetchone()[0] >= 3
    # 出力 → そのまま取り込み直しても「変更なし」
    out = c.get("/excel/materials.xlsx")
    pv = excel.parse(conn, out.data)
    assert {m["action"] for m in pv["materials"]} == {"same"}
