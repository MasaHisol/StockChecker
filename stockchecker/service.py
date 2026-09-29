"""定期チェックの本体: 価格取得 → 判定 → アラート → 見積依頼メール作成/自動送信。"""
import json
import logging
import urllib.request
from collections import defaultdict
from datetime import date

from . import db, mailer, rules

log = logging.getLogger(__name__)


def queue_email(conn, kind, to_addr, subject, body, material_id=None,
                supplier_id=None, cc_addr=None):
    cur = conn.execute(
        "INSERT INTO emails (kind, to_addr, cc_addr, subject, body, material_id, supplier_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, to_addr, cc_addr, subject, body, material_id, supplier_id))
    conn.commit()
    return cur.lastrowid


def send_email(conn, email_id, settings):
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None or row["status"] == "sent":
        return False
    try:
        where = mailer.deliver(mailer.to_message(row, settings), settings)
    except Exception as e:  # SMTP エラー等は記録して継続
        log.exception("send failed")
        conn.execute("UPDATE emails SET status='failed', error=? WHERE id=?", (str(e), email_id))
        conn.commit()
        return False
    conn.execute(
        "UPDATE emails SET status='sent', error=?, sent_at=datetime('now','localtime') WHERE id=?",
        (f"delivered: {where}", email_id))
    conn.commit()
    return True


def create_rfq(conn, material, supplier, settings, findings=()):
    subject, body = mailer.build_rfq(material, supplier, settings, findings)
    owner = conn.execute("SELECT * FROM staff WHERE id = ?", (material["owner_id"],)).fetchone()
    return queue_email(conn, "rfq", supplier["email"], subject, body,
                       material["id"], supplier["id"],
                       cc_addr=owner["email"] if owner else None)


def create_order(conn, material, supplier, settings, quantity, unit_price, delivery_date):
    subject, body = mailer.build_order(material, supplier, settings, quantity,
                                       unit_price, delivery_date)
    owner = conn.execute("SELECT * FROM staff WHERE id = ?", (material["owner_id"],)).fetchone()
    return queue_email(conn, "order", supplier["email"], subject, body,
                       material["id"], supplier["id"],
                       cc_addr=owner["email"] if owner else None)


def refresh_prices(conn, providers, materials):
    count = 0
    for m in materials:
        for p in providers:
            try:
                for obs in p.fetch(m):
                    db.add_observation(
                        conn, m["id"], p.name,
                        unit_price=obs.get("unit_price"),
                        lead_time_days=obs.get("lead_time_days"),
                        stock_qty=obs.get("stock_qty"),
                        min_order_qty=obs.get("min_order_qty"),
                        currency=obs.get("currency", "JPY"))
                    count += 1
            except Exception:
                log.exception("provider %s failed for %s", p.name, m["part_number"])
    return count


def evaluate_material(conn, material, settings, today=None):
    latest = db.latest_observation(conn, material["id"])
    prev = db.previous_observation(conn, material["id"], latest["id"]) if latest else None
    return rules.evaluate(material, latest, prev, settings.rules, today)


def _recent_rfq_exists(conn, material_id, supplier_id, days):
    return conn.execute(
        "SELECT 1 FROM emails WHERE kind='rfq' AND material_id=? AND supplier_id=? "
        "AND (status='draft' OR created_at >= datetime('now','localtime', ?))",
        (material_id, supplier_id, f"-{days} days")).fetchone() is not None


def run_checks(conn, settings, providers=(), today=None, notify=True):
    """全部材を点検し、結果サマリを返す。cron などから定期実行する想定。"""
    today = today or date.today()
    materials = conn.execute("SELECT * FROM materials WHERE active = 1").fetchall()
    summary = {"materials": len(materials), "observations": 0, "new_alerts": 0,
               "rfq_drafts": 0, "rfq_sent": 0, "notifications": 0}
    if providers:
        summary["observations"] = refresh_prices(conn, providers, materials)

    per_owner = defaultdict(list)
    for m in materials:
        findings = evaluate_material(conn, m, settings, today)
        codes = {f.code for f in findings}
        open_alerts = {a["kind"]: a for a in conn.execute(
            "SELECT * FROM alerts WHERE material_id=? AND status='open'", (m["id"],))}
        # 解消した条件のアラートはクローズ
        for kind, a in open_alerts.items():
            if kind not in codes:
                conn.execute("UPDATE alerts SET status='resolved' WHERE id=?", (a["id"],))
        new = [f for f in findings if f.code not in open_alerts]
        for f in new:
            conn.execute("INSERT INTO alerts (material_id, kind, message) VALUES (?, ?, ?)",
                         (m["id"], f.code, f.message))
        conn.commit()
        summary["new_alerts"] += len(new)
        if new and m["owner_id"]:
            per_owner[m["owner_id"]].append((m, new))

        if rules.needs_quote(findings) and m["preferred_supplier_id"]:
            sup = conn.execute("SELECT * FROM suppliers WHERE id=?",
                               (m["preferred_supplier_id"],)).fetchone()
            if sup and not _recent_rfq_exists(conn, m["id"], sup["id"],
                                              settings.rules.stale_days):
                eid = create_rfq(conn, m, sup, settings, findings)
                summary["rfq_drafts"] += 1
                if settings.auto_send_enabled and sup["auto_send_rfq"]:
                    if send_email(conn, eid, settings):
                        summary["rfq_sent"] += 1

    if notify:
        for owner_id, items in per_owner.items():
            owner = conn.execute("SELECT * FROM staff WHERE id=?", (owner_id,)).fetchone()
            if owner is None:
                continue
            notify_owner(conn, owner, items, settings)
            summary["notifications"] += 1
    return summary


def notify_owner(conn, owner, items, settings):
    subject, body = mailer.build_alert(owner, items, settings)
    eid = queue_email(conn, "alert", owner["email"], subject, body)
    send_email(conn, eid, settings)
    if owner["slack_webhook"]:
        try:
            req = urllib.request.Request(
                owner["slack_webhook"],
                data=json.dumps({"text": f"*{subject}*\n{body}"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            log.exception("slack notify failed")
    ids = [a["id"] for m, _ in items for a in conn.execute(
        "SELECT id FROM alerts WHERE material_id=? AND status='open' AND notified_at IS NULL",
        (m["id"],))]
    conn.executemany("UPDATE alerts SET notified_at=datetime('now','localtime') WHERE id=?",
                     [(i,) for i in ids])
    conn.commit()
