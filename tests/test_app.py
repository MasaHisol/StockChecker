from stockchecker import create_app, db, service
from stockchecker.config import Settings


def make(tmp_path, auto=False):
    s = Settings(database=str(tmp_path / "t.db"), outbox_dir=str(tmp_path / "out"),
                 auto_send_enabled=auto)
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
    # 2 回目は重複しない
    r = service.run_checks(conn, s)
    assert r["new_alerts"] == 0 and r["rfq_drafts"] == 0
    # 価格が入ればアラートは解消
    db.add_observation(conn, 1, "manual", 100, 3)
    service.run_checks(conn, s)
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0] == 0


def test_auto_send(tmp_path):
    _, s, conn = make(tmp_path, auto=True)
    r = service.run_checks(conn, s)
    assert r["rfq_sent"] == 1
    assert len(list((tmp_path / "out").glob("*.eml"))) == 2  # RFQ + 担当者通知


def test_pages(tmp_path):
    app, s, conn = make(tmp_path)
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.post("/run-checks", data={"demo": "1"}).status_code == 302
    r = c.post("/materials/1/rfq", data={"supplier_id": 1})
    assert r.status_code == 302
    for url in ["/", "/materials/1", "/emails", "/masters", "/import", "/materials/new", "/sources",
                "/materials/1/edit", r.headers["Location"]]:
        assert c.get(url).status_code == 200, url
    r = c.post("/materials/1/order", data={"supplier_id": 1, "quantity": 5, "unit_price": "100"})
    assert r.status_code == 302
    r = c.post("/import", data={"text": "part_number,unit_price,lead_time_days\nP-1,120,4\nX,1,1"})
    assert r.status_code == 302
    r = c.post("/emails/1", data={"to_addr": "s@example.com", "subject": "件名", "body": "本文",
                                  "action": "send"})
    assert r.status_code == 302
    assert conn.execute("SELECT status FROM emails WHERE id=1").fetchone()[0] == "sent"
