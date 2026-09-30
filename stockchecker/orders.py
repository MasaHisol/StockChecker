"""発注・入荷の管理。

発注 → (仕入先の納期回答) → 入荷 (分納可) の流れを記録し、
「納期回答が無い」「入荷予定日を過ぎた」発注をリマインドする。
"""
from datetime import date, timedelta

from . import db, rules

STATUS = {"ordered": "発注済 (納期回答待ち)", "confirmed": "納期回答あり", "partial": "一部入荷",
          "received": "入荷済", "cancelled": "キャンセル"}
OPEN = ("ordered", "confirmed", "partial")
ORDER_ALERTS = ("order_no_eta", "delivery_late")


def _today():
    return date.today().isoformat()


def get(conn, oid):
    return conn.execute(
        "SELECT o.*, m.part_number, m.name AS material_name, m.unit, m.owner_id, s.name AS supplier_name, "
        "s.email AS supplier_email FROM orders o JOIN materials m ON m.id=o.material_id "
        "LEFT JOIN suppliers s ON s.id=o.supplier_id WHERE o.id=?", (oid,)).fetchone()


def listing(conn, where="1=1", args=()):
    return conn.execute(
        "SELECT o.*, m.part_number, m.name AS material_name, m.unit, m.owner_id, st.name AS owner_name, "
        "s.name AS supplier_name, COALESCE(o.promised_date, o.required_date) AS due_date "
        "FROM orders o JOIN materials m ON m.id=o.material_id LEFT JOIN suppliers s ON s.id=o.supplier_id "
        f"LEFT JOIN staff st ON st.id=m.owner_id WHERE {where} "
        "ORDER BY o.status IN ('received','cancelled'), COALESCE(o.promised_date, o.required_date, '9999'), o.id DESC",
        args).fetchall()


def create(conn, material_id, supplier_id, quantity, unit_price=None, order_date=None, required_date=None,
           email_id=None, note=None, user_id=None, actor=None, client=None):
    order_date = order_date or _today()
    oid = conn.execute(
        "INSERT INTO orders (material_id, supplier_id, email_id, quantity, unit_price, order_date, "
        "required_date, note, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (material_id, supplier_id, email_id, int(quantity), unit_price, order_date, required_date or None,
         note, user_id)).lastrowid
    conn.commit()
    if unit_price is not None:  # 実際の購入単価も価格の履歴に残す
        db.add_observation(conn, material_id, "order", unit_price, None, supplier_id=supplier_id,
                           observed_at=f"{order_date} 12:00:00", created_by=user_id)
    db.log_activity(conn, user_id, "order_new", material_id,
                    f"発注 {quantity} 個" + (f" @ ¥{unit_price:,.0f}" if unit_price is not None else "")
                    + (f" / 希望納期 {required_date}" if required_date else ""), actor, client)
    return oid


def create_from_email(conn, email, user_id=None, actor=None, client=None):
    """送信した注文メールから、部材ごとの発注を登録する。"""
    ids = []
    for r in conn.execute("SELECT em.*, m.quantity AS default_qty, m.required_date FROM email_materials em "
                          "JOIN materials m ON m.id=em.material_id WHERE em.email_id=? AND em.order_id IS NULL",
                          (email["id"],)).fetchall():
        oid = create(conn, r["material_id"], email["supplier_id"], r["quantity"] or r["default_qty"],
                     r["unit_price"], required_date=_as_date(r["requested_date"]) or r["required_date"],
                     email_id=email["id"], user_id=user_id, actor=actor, client=client)
        conn.execute("UPDATE email_materials SET order_id=? WHERE email_id=? AND material_id=?",
                     (oid, email["id"], r["material_id"]))
        ids.append(oid)
    conn.commit()
    return ids


def _as_date(s):
    try:
        return date.fromisoformat(str(s)[:10]).isoformat() if s else None
    except ValueError:
        return None


def set_eta(conn, oid, promised_date, user_id=None, actor=None, client=None, note=None):
    o = get(conn, oid)
    conn.execute("UPDATE orders SET promised_date=?, status=CASE WHEN status='ordered' THEN 'confirmed' "
                 "ELSE status END, updated_at=datetime('now','localtime') WHERE id=?", (promised_date, oid))
    # 納期確認のメールに回答があったことにする
    for r in conn.execute("SELECT e.id FROM emails e JOIN email_materials em ON em.email_id=e.id "
                          "WHERE em.order_id=? AND e.answered_at IS NULL AND e.status='sent'", (oid,)).fetchall():
        conn.execute("UPDATE emails SET answered_at=datetime('now','localtime') WHERE id=?", (r["id"],))
    conn.commit()
    db.log_activity(conn, user_id, "order_eta", o["material_id"],
                    f"納期回答 {promised_date}" + (f" (前回 {o['promised_date']})" if o["promised_date"] else "")
                    + (f" / {note}" if note else ""), actor, client)
    resolve(conn, oid)


def receive(conn, oid, quantity, received_date=None, note=None, user_id=None, actor=None, client=None):
    o = get(conn, oid)
    received_date = received_date or _today()
    conn.execute("INSERT INTO receipts (order_id, quantity, received_date, note, actor) VALUES (?,?,?,?,?)",
                 (oid, int(quantity), received_date, note, actor))
    total = o["received_qty"] + int(quantity)
    status = "received" if total >= o["quantity"] else "partial"
    conn.execute("UPDATE orders SET received_qty=?, received_date=?, status=?, updated_at=datetime('now','localtime') "
                 "WHERE id=?", (total, received_date, status, oid))
    conn.commit()
    db.log_activity(conn, user_id, "order_receive", o["material_id"],
                    f"入荷 {quantity} {o['unit']} ({total}/{o['quantity']})" + (" ・ 完納" if status == "received" else "")
                    + (f" / {note}" if note else ""), actor, client)
    resolve(conn, oid)
    return status


def cancel(conn, oid, note=None, user_id=None, actor=None, client=None):
    o = get(conn, oid)
    conn.execute("UPDATE orders SET status='cancelled', updated_at=datetime('now','localtime') WHERE id=?", (oid,))
    conn.commit()
    db.log_activity(conn, user_id, "order_cancel", o["material_id"], note or "発注をキャンセル", actor, client)
    resolve(conn, oid)


def resolve(conn, oid):
    """状態が変わった発注のリマインドを片付ける (必要なら次のスキャンで出し直す)。"""
    conn.execute(f"UPDATE alerts SET status='resolved' WHERE ref_order_id=? AND kind IN {ORDER_ALERTS} "
                 "AND status='open'", (oid,))
    conn.commit()


def scan(conn, cfg, today=None):
    """納期回答待ち・入荷遅れのリマインドを作る。戻り値: 新しく出したもの [(material, Finding)]"""
    today = today or date.today()
    limit = (today - timedelta(days=int(cfg["followup_days"]))).isoformat()
    new, active = [], set()
    for o in listing(conn, f"o.status IN {OPEN}"):
        m = conn.execute("SELECT * FROM materials WHERE id=?", (o["material_id"],)).fetchone()
        due = o["due_date"]
        if due and due < today.isoformat():
            kind = "delivery_late"
            msg = (f"{o['supplier_name'] or '仕入先'}への発注 ({o['order_date']}・{o['quantity']}{o['unit']}) の"
                   f"入荷予定日 {due} を過ぎています ({o['received_qty']}/{o['quantity']} 入荷済)。")
        elif o["status"] == "ordered" and o["order_date"] <= limit:
            kind = "order_no_eta"
            msg = (f"{o['order_date']} に{o['supplier_name'] or '仕入先'}へ発注した分の納期回答がありません。"
                   "納期確認のメールを送るか、回答納期を記録してください。")
        else:
            continue
        active.add((o["id"], kind))
        a = conn.execute("SELECT * FROM alerts WHERE ref_order_id=? AND kind=? AND status='open'",
                         (o["id"], kind)).fetchone()
        if a:
            if a["message"] != msg:
                conn.execute("UPDATE alerts SET message=? WHERE id=?", (msg, a["id"]))
            continue
        conn.execute("INSERT INTO alerts (material_id, kind, message, ref_order_id) VALUES (?,?,?,?)",
                     (o["material_id"], kind, msg, o["id"]))
        new.append((m, [rules.Finding(kind, msg, False)]))
    for a in conn.execute(f"SELECT * FROM alerts WHERE kind IN {ORDER_ALERTS} AND status='open'").fetchall():
        if (a["ref_order_id"], a["kind"]) not in active:
            conn.execute("UPDATE alerts SET status='resolved' WHERE id=?", (a["id"],))
    conn.commit()
    return new
