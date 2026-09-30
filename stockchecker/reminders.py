"""価格・納期の確認周期と、回答待ちメールのリマインド。

起動中のアプリが一定間隔 (既定 30 分) でスキャンする。価格の取得 (ネット) は行わない。
- 確認期限を過ぎた部材 → 担当者にリマインド (設定により確認メールを自動送信)
- 送信後 N 日回答が無いメール → 督促のリマインド (設定により督促メールを自動送信)
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import mailflow, rules, service

log = logging.getLogger(__name__)
REMINDER_KINDS = {"confirm_due", "no_reply"}


def _dt(s):
    return datetime.fromisoformat(str(s)[:19].replace("T", " ")) if s else None


def effective_interval(m, cfg):
    """部材の確認周期 (日)。0 は確認しない。"""
    if m["confirm_interval_days"] is not None:
        return int(m["confirm_interval_days"])
    has_url = bool((m["watch_urls"] or "").strip())
    return 0 if has_url and not m["custom_item"] else int(cfg["confirm_interval_days"])


def confirm_status(conn, m, cfg, now=None):
    """(周期, 前回確認日時, 次回期限) を返す。周期 0 なら期限は None。"""
    now = now or datetime.now()
    interval = effective_interval(m, cfg)
    base = _dt(m["last_confirmed_at"])
    if base is None:
        q = conn.execute("SELECT MAX(observed_at) FROM price_observations WHERE material_id=? "
                         "AND source IN ('quote','manual','csv')", (m["id"],)).fetchone()[0]
        base = _dt(q) or _dt(m["created_at"])
    due = base + timedelta(days=interval) if interval > 0 and base else None
    return interval, base, due


def _open_alert(conn, material_id, kind):
    return conn.execute("SELECT * FROM alerts WHERE material_id=? AND kind=? AND status='open'",
                        (material_id, kind)).fetchone()


def _ensure_alert(conn, material_id, kind, message, ref_email_id=None):
    a = _open_alert(conn, material_id, kind)
    if a:
        if a["message"] != message:
            conn.execute("UPDATE alerts SET message=?, ref_email_id=? WHERE id=?",
                         (message, ref_email_id, a["id"]))
        return False
    conn.execute("INSERT INTO alerts (material_id, kind, message, ref_email_id) VALUES (?,?,?,?)",
                 (material_id, kind, message, ref_email_id))
    return True


def _pending_email(conn, material_id, since):
    """確認・見積依頼で回答待ちのメール (since 以降に作成) があるか。"""
    return conn.execute(
        "SELECT e.* FROM emails e JOIN email_materials em ON em.email_id=e.id "
        f"WHERE em.material_id=? AND e.kind IN {mailflow.AWAIT_REPLY} AND e.answered_at IS NULL "
        "AND e.status IN ('draft','sent') AND e.created_at >= ? ORDER BY e.id DESC LIMIT 1",
        (material_id, since.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()


def scan(conn, settings, now=None):
    now = now or datetime.now()
    cfg = mailflow.reminder_config(conn)
    can_auto_send = bool(settings.smtp_host)  # 自動送信は SMTP 設定がある場合のみ
    summary = {"confirm_due": 0, "auto_confirm": 0, "no_reply": 0, "auto_followup": 0,
               "notifications": 0}
    new_items = defaultdict(list)   # owner_id -> [(material, [Finding])]

    # 1. 確認期限
    for m in conn.execute("SELECT * FROM materials WHERE active=1").fetchall():
        interval, base, due = confirm_status(conn, m, cfg, now)
        if not due or now < due:
            a = _open_alert(conn, m["id"], "confirm_due")
            if a:
                conn.execute("UPDATE alerts SET status='resolved' WHERE id=?", (a["id"],))
            continue
        if _pending_email(conn, m["id"], base):
            continue  # 既に確認中 (回答待ちは督促の対象)
        sup = conn.execute("SELECT * FROM suppliers WHERE id=?",
                           (m["preferred_supplier_id"],)).fetchone() if m["preferred_supplier_id"] else None
        if sup and m["auto_confirm"] and cfg["auto_confirm_enabled"] == "1" and can_auto_send:
            eid = mailflow.create(conn, "confirm", sup, [m], settings, auto=True)
            if service.send_email(conn, eid, settings):
                summary["auto_confirm"] += 1
                continue
        msg = (f"価格・納期の確認時期です (前回確認 {base:%Y-%m-%d}、周期 {interval} 日)。"
               + ("確認メールを作成してください。" if sup else "主仕入先を設定すると確認メールをワンタッチで作れます。"))
        if _ensure_alert(conn, m["id"], "confirm_due", msg):
            summary["confirm_due"] += 1
            if m["owner_id"]:
                new_items[m["owner_id"]].append((m, [rules.Finding("confirm_due", msg, False)]))

    # 2. 回答待ち
    limit = (now - timedelta(days=int(cfg["followup_days"]))).strftime("%Y-%m-%d %H:%M:%S")
    waiting = conn.execute(
        f"SELECT e.* FROM emails e WHERE e.kind IN {mailflow.AWAIT_REPLY} AND e.status='sent' "
        "AND e.answered_at IS NULL AND e.sent_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM emails f WHERE f.followup_of=e.id)", (limit,)).fetchall()
    for e in waiting:
        mats = mailflow.linked_materials(conn, e["id"])
        # 自動の督促は元メール 1 通につき 1 回だけ (督促への督促はしない)
        if cfg["auto_followup_enabled"] == "1" and can_auto_send and e["kind"] != "followup":
            fid = mailflow.create_followup(conn, e, settings, auto=True)
            if fid and service.send_email(conn, fid, settings):
                summary["auto_followup"] += 1
                continue
        msg = (f"{e['sent_at'][:10]} に送った「{e['subject']}」に回答がありません。"
               "督促メールを作成するか、回答があれば「回答あり」を記録してください。")
        for m in mats:
            if _ensure_alert(conn, m["id"], "no_reply", msg, e["id"]):
                summary["no_reply"] += 1
                if m["owner_id"]:
                    new_items[m["owner_id"]].append((m, [rules.Finding("no_reply", msg, False)]))
    conn.commit()

    if cfg["notify_owner"] == "1":
        for owner_id, items in new_items.items():
            owner = conn.execute("SELECT * FROM staff WHERE id=?", (owner_id,)).fetchone()
            if owner and owner["email"]:
                service.notify_owner(conn, owner, items, settings)
                summary["notifications"] += 1
    return summary
