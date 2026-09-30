"""仕入先とのメール業務: 見積依頼・価格/納期確認・注文・回答督促。

- 文面はテンプレート (画面から編集可) に部材情報などを差し込んで作る
- 複数の部材を 1 通にまとめられる (仕入先ごと)
- 送信したメールの「回答あり」を記録し、回答が無いものは督促の対象になる
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import db

KINDS = {
    "rfq": "見積依頼",
    "confirm": "価格・納期の確認",
    "order": "注文",
    "followup": "回答のお願い (督促)",
    "delivery": "納期の確認 (発注済み)",
    "alert": "担当者への通知",
}
# 回答を待つ種類 (回答が無いと督促のリマインド対象)
AWAIT_REPLY = ("rfq", "confirm", "followup", "delivery")

PLACEHOLDERS = {
    "{supplier_name}": "仕入先の社名", "{contact_name}": "仕入先のご担当者名",
    "{company}": "自社名", "{sender_name}": "差出人 (ログイン中の担当者)", "{sender_email}": "差出人メール",
    "{items}": "部材の一覧 (品名・品番・数量・希望納期 など)", "{title}": "件名用の部材名",
    "{reply_by}": "回答期限 (今日 + 設定日数)", "{delivery_date}": "注文の納期",
    "{total_amount}": "注文の合計金額", "{original_subject}": "督促: 元メールの件名",
    "{original_date}": "督促: 元メールの送信日",
    "{order_date}": "納期確認: 発注日",
}

DEFAULT_TEMPLATES = {
    "rfq": (
        "【見積依頼】{title} - {company}",
        "{supplier_name}\n{contact_name} 様\n\n"
        "いつもお世話になっております。{company}の{sender_name}です。\n\n"
        "下記部材につきまして、お見積りをお願いいたします。\n\n"
        "{items}\n\n"
        "お手数ですが、以下をご回答いただけますと幸いです。\n"
        "  ・単価 (税抜) および見積有効期限\n"
        "  ・納期 (受注後の日数、または最短入荷日)\n"
        "  ・最小発注数量 / 在庫状況\n\n"
        "恐れ入りますが、{reply_by} までにご回答いただけますと幸いです。\n"
        "ご多用のところ恐縮ですが、よろしくお願いいたします。\n\n"
        "--\n{company}\n{sender_name}\n{sender_email}\n"),
    "confirm": (
        "【価格・納期ご確認のお願い】{title} - {company}",
        "{supplier_name}\n{contact_name} 様\n\n"
        "いつもお世話になっております。{company}の{sender_name}です。\n\n"
        "以前お取引・お見積りいただいた下記部材につきまして、\n"
        "現在の価格・納期に変更がないかご確認をお願いいたします。\n\n"
        "{items}\n\n"
        "変更がない場合も、その旨ご返信いただけますと幸いです。\n"
        "恐れ入りますが、{reply_by} までにご回答をお願いいたします。\n\n"
        "--\n{company}\n{sender_name}\n{sender_email}\n"),
    "order": (
        "【注文書】{title} - {company}",
        "{supplier_name}\n{contact_name} 様\n\n"
        "いつもお世話になっております。{company}の{sender_name}です。\n\n"
        "お見積りいただきました下記部材につきまして、以下のとおり注文いたします。\n\n"
        "{items}\n\n"
        "  合計金額: {total_amount} 円 (税抜)\n"
        "  納期　　: {delivery_date}\n\n"
        "恐れ入りますが、注文請書または受領のご連絡をお願いいたします。\n\n"
        "--\n{company}\n{sender_name}\n{sender_email}\n"),
    "delivery": (
        "【納期ご確認のお願い】{title} - {company}",
        "{supplier_name}\n{contact_name} 様\n\n"
        "いつもお世話になっております。{company}の{sender_name}です。\n\n"
        "{order_date} に発注いたしました下記部材につきまして、\n"
        "納期 (出荷予定日・入荷予定日) をお知らせいただけますでしょうか。\n\n"
        "{items}\n\n"
        "恐れ入りますが、{reply_by} までにご回答いただけますと幸いです。\n"
        "既にご連絡いただいている場合は、行き違いとなり失礼いたしました。\n\n"
        "--\n{company}\n{sender_name}\n{sender_email}\n"),
    "followup": (
        "【ご確認のお願い】{original_subject}",
        "{supplier_name}\n{contact_name} 様\n\n"
        "いつもお世話になっております。{company}の{sender_name}です。\n\n"
        "{original_date} にお送りしました下記のご依頼につきまして、\n"
        "その後のご状況はいかがでしょうか。\n\n"
        "{items}\n\n"
        "お忙しいところ恐縮ですが、{reply_by} までにご回答いただけますと幸いです。\n"
        "既にご回答いただいている場合は、行き違いとなり失礼いたしました。\n\n"
        "--\n{company}\n{sender_name}\n{sender_email}\n"),
}

REMINDER_DEFAULTS = {
    "confirm_interval_days": "90",   # URL の無い部材 (特注品・メール発注) の既定の確認周期。0 で無効
    "followup_days": "5",            # 送信後この日数で回答が無ければ督促をリマインド
    "reply_days": "5",               # 文面の「◯日までにご回答を」の日数
    "auto_confirm_enabled": "0",     # 確認メールの自動送信を許可 (部材ごとの設定と両方 ON で送信)
    "auto_followup_enabled": "0",    # 回答が無いとき督促メールを自動送信
    "notify_owner": "1",             # リマインドを担当者にメールで通知
}


def reminder_config(conn):
    return {k: db.get_setting(conn, k, v) for k, v in REMINDER_DEFAULTS.items()}


def get_template(conn, kind):
    r = conn.execute("SELECT subject, body FROM mail_templates WHERE kind=?", (kind,)).fetchone()
    return (r["subject"], r["body"]) if r else DEFAULT_TEMPLATES[kind]


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fmt_price(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v < 100 else f"{v:,.0f}"


def item_lines(conn, kind, m, qty=None, unit_price=None):
    lines = [f"■ {m['name']}", f"  品番　　: {m['part_number']}"]
    if m["maker"]:
        lines.append(f"  メーカー: {m['maker']}")
    if m["spec"]:
        lines.append(f"  仕様　　: {m['spec']}")
    lines.append(f"  数量　　: {qty or m['quantity']} {m['unit']}")
    if m["required_date"] and kind not in ("order", "delivery"):
        lines.append(f"  希望納期: {m['required_date']}")
    if kind == "confirm":
        last = db.latest_observation(conn, m["id"])
        if last and last["unit_price"] is not None:
            lines.append(f"  前回単価: {_fmt_price(last['unit_price'])} 円 ({last['observed_at'][:10]} 時点)")
        if last and last["lead_time_days"] is not None:
            lines.append(f"  前回納期: {last['lead_time_days']} 日")
    if kind == "order" and unit_price is not None:
        lines.append(f"  単価　　: {_fmt_price(unit_price)} 円 (税抜)")
        lines.append(f"  金額　　: {unit_price * (qty or m['quantity']):,.0f} 円 (税抜)")
    return "\n".join(lines)


def sender_of(conn, user_id, settings):
    u = conn.execute("SELECT * FROM staff WHERE id=?", (user_id,)).fetchone() if user_id else None
    if u:
        return {"name": u["name"], "email": u["email"]}
    return {"name": settings.purchaser_name, "email": settings.purchaser_email}


def build(conn, kind, supplier, materials, settings, sender, extra=None):
    """文面を作る。materials は [(material_row, qty, unit_price)]。"""
    cfg = reminder_config(conn)
    subject_t, body_t = get_template(conn, kind)
    first = materials[0][0]
    title = f"{first['name']} ({first['part_number']})"
    if len(materials) > 1:
        title = f"{first['name']} 他 {len(materials) - 1} 件"
    total = sum((up or 0) * (q or m["quantity"]) for m, q, up in materials)
    ctx = _Safe({
        "supplier_name": supplier["name"], "contact_name": supplier["contact_name"] or "ご担当者",
        "company": settings.company_name, "sender_name": sender["name"], "sender_email": sender["email"],
        "items": "\n\n".join(item_lines(conn, kind, m, q, up) for m, q, up in materials),
        "title": title, "total_amount": f"{total:,.0f}",
        "reply_by": (date.today() + timedelta(days=int(cfg["reply_days"]))).strftime("%Y年%m月%d日"),
        "delivery_date": "別途ご相談",
    })
    ctx.update(extra or {})
    return subject_t.format_map(ctx), body_t.format_map(ctx)


def create(conn, kind, supplier, materials, settings, user_id=None, cc=None, extra=None,
           followup_of=None, auto=False, actor=None, client=None, order_id=None):
    """下書きを作成して email_id を返す。materials は material_row のリストか
    (material_row, qty, unit_price) のリスト。"""
    items = [x if isinstance(x, tuple) else (x, None, None) for x in materials]
    sender = sender_of(conn, user_id, settings) if not auto or user_id else \
        sender_of(conn, items[0][0]["owner_id"], settings)
    subject, body = build(conn, kind, supplier, items, settings, sender, extra)
    if cc is None:  # 担当者を CC に入れる (重複は除く)
        owners = {r["email"] for r in conn.execute(
            f"SELECT email FROM staff WHERE id IN ({','.join('?' * len(items))})",
            [m["owner_id"] for m, _, _ in items]) if r["email"]}
        cc = ", ".join(sorted(owners)) or None
    cur = conn.execute(
        "INSERT INTO emails (kind, to_addr, cc_addr, subject, body, material_id, supplier_id, "
        "created_by, followup_of, auto) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, supplier["email"], cc, subject, body,
         items[0][0]["id"] if len(items) == 1 else None, supplier["id"], user_id, followup_of,
         1 if auto else 0))
    eid = cur.lastrowid
    req = (extra or {}).get("delivery_date") if kind == "order" else None
    conn.executemany("INSERT OR IGNORE INTO email_materials (email_id, material_id, quantity, unit_price, "
                     "requested_date, order_id) VALUES (?,?,?,?,?,?)",
                     [(eid, m["id"], q, up, req, order_id) for m, q, up in items])
    conn.commit()
    for m, _, _ in items:
        db.log_activity(conn, user_id, "email_draft", m["id"],
                        f"{KINDS[kind]}の下書きを作成 ({supplier['name']})" + (" [自動]" if auto else ""),
                        actor if not auto else "自動", client)
    return eid


def create_delivery_inquiry(conn, order, settings, user_id=None, actor=None, client=None, auto=False):
    """発注済みの部材について、仕入先に納期を問い合わせる下書きを作る。"""
    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (order["supplier_id"],)).fetchone()
    m = conn.execute("SELECT * FROM materials WHERE id=?", (order["material_id"],)).fetchone()
    if not sup or not m:
        return None
    return create(conn, "delivery", sup, [(m, order["quantity"], None)], settings, user_id,
                  extra={"order_date": order["order_date"]}, auto=auto, actor=actor, client=client,
                  order_id=order["id"])


def create_bulk(conn, kind, materials, settings, user_id=None, actor=None, client=None):
    """部材を主仕入先ごとにまとめて下書きを作る。戻り値: (email_ids, 仕入先未設定の部材)"""
    groups, no_supplier = defaultdict(list), []
    for m in materials:
        (groups[m["preferred_supplier_id"]] if m["preferred_supplier_id"] else no_supplier).append(m)
    ids = []
    for sid, ms in groups.items():
        sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if sup:
            items = [(m, None, _latest_price(conn, m)) if kind == "order" else m for m in ms]
            ids.append(create(conn, kind, sup, items, settings, user_id, actor=actor, client=client))
    return ids, no_supplier


def _latest_price(conn, m):
    o = db.latest_observation(conn, m["id"])
    return o["unit_price"] if o else None


def create_followup(conn, email, settings, user_id=None, auto=False, actor=None, client=None):
    """送信済みメールへの督促の下書きを作る。"""
    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (email["supplier_id"],)).fetchone()
    mats = linked_materials(conn, email["id"])
    if not sup or not mats:
        return None
    extra = {"original_subject": email["subject"],
             "original_date": (email["sent_at"] or email["created_at"])[:10]}
    oid = conn.execute("SELECT order_id FROM email_materials WHERE email_id=? AND order_id IS NOT NULL",
                       (email["id"],)).fetchone()
    return create(conn, "followup", sup, mats, settings, user_id, extra=extra,
                  followup_of=email["id"], auto=auto, actor=actor, client=client,
                  order_id=oid[0] if oid else None)


def linked_materials(conn, email_id):
    return conn.execute("SELECT m.* FROM email_materials em JOIN materials m ON m.id=em.material_id "
                        "WHERE em.email_id=? ORDER BY m.id", (email_id,)).fetchall()


def mark_answered(conn, email_id, user_id=None, actor=None, client=None, detail=None):
    """メール (と督促の元メール) を回答済みにし、部材の確認日を更新する。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chain, eid = [], email_id
    while eid:  # 督促 → 元メール をたどって全部回答済みにする
        e = conn.execute("SELECT id, followup_of FROM emails WHERE id=?", (eid,)).fetchone()
        if not e or e["id"] in chain:
            break
        chain.append(e["id"])
        eid = e["followup_of"]
    chain += [r["id"] for r in conn.execute(
        f"SELECT id FROM emails WHERE followup_of IN ({','.join('?' * len(chain))})", chain)]
    conn.executemany("UPDATE emails SET answered_at=? WHERE id=? AND answered_at IS NULL",
                     [(now, i) for i in chain])
    for m in linked_materials(conn, email_id):
        conn.execute("UPDATE materials SET last_confirmed_at=? WHERE id=?", (now, m["id"]))
        db.log_activity(conn, user_id, "answered", m["id"], detail or "仕入先からの回答を記録", actor, client)
    conn.execute("UPDATE alerts SET status='resolved' WHERE ref_email_id IN "
                 f"({','.join('?' * len(chain))}) AND status='open'", chain)
    conn.commit()


def record_quote_answer(conn, material_id, user_id=None, actor=None, client=None):
    """見積回答 (価格・納期) を記録したとき、その部材の回答待ちメールを回答済みにする。"""
    pending = conn.execute(
        "SELECT e.id FROM emails e JOIN email_materials em ON em.email_id=e.id "
        f"WHERE em.material_id=? AND e.kind IN {AWAIT_REPLY} AND e.status='sent' "
        "AND e.answered_at IS NULL", (material_id,)).fetchall()
    for r in pending:
        mark_answered(conn, r["id"], user_id, actor, client)
    conn.execute("UPDATE materials SET last_confirmed_at=datetime('now','localtime') WHERE id=?",
                 (material_id,))
    conn.execute("UPDATE alerts SET status='resolved' WHERE material_id=? AND kind='confirm_due' "
                 "AND status='open'", (material_id,))
    conn.commit()
