import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stockchecker import create_app, db, mailflow, reminders, service
from stockchecker.config import Settings


def make(tmp_path, auto=False, **kw):
    s = Settings(database=str(tmp_path / "t.db"), outbox_dir=str(tmp_path / "out"),
                 auto_send_enabled=auto, auth=False, csrf=False, run_jobs_async=False,
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
                "/sources", "/account", r.headers["Location"]]:
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


def test_login_setup_and_csrf(tmp_path):
    s = Settings(database=str(tmp_path / "t.db"), outbox_dir=str(tmp_path / "o"))
    app = create_app(s)
    c = app.test_client()
    assert "/setup" in c.get("/").headers["Location"]
    c.post("/setup", data={"name": "管理者", "email": "a@example.com", "password": "secret1"})
    assert c.get("/").status_code == 200
    c.post("/logout", data={"_csrf": _token(c)})
    assert "/login" in c.get("/materials").headers["Location"]
    r = c.post("/login", data={"email": "a@example.com", "password": "wrong!"})
    assert "違います" in r.get_data(as_text=True)
    c.post("/login", data={"email": "A@example.com", "password": "secret1"})
    assert c.get("/materials").status_code == 200
    # CSRF トークンが無い POST は拒否
    assert c.post("/materials/new", data={"part_number": "X", "name": "x"}).status_code == 400
    r = c.post("/materials/new", data={"part_number": "X", "name": "x", "_csrf": _token(c)})
    assert r.status_code == 302
    # メンバー追加 → そのメンバーでログインできる / 設定画面は管理者のみ
    c.post("/masters", data={"type": "staff", "name": "メンバー", "email": "m@example.com",
                             "password": "member1", "role": "member", "_csrf": _token(c)})
    c2 = app.test_client()
    c2.post("/login", data={"email": "m@example.com", "password": "member1"})
    assert c2.get("/").status_code == 200
    assert c2.get("/settings").status_code == 302


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
