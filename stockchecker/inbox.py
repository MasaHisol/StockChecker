"""仕入先からの返信メールの取り込み。

- 取り込み方法: .eml / .msg ファイルのアップロード、またはメールサーバー (IMAP) から取得
  (IMAP では「こちらが送ったメールへの返信」または「登録済み仕入先からのメール」だけを保存する)
- 送ったメールとの対応付け: In-Reply-To / References → 件名 → 差出人 + 本文中の品番 の順に判定
- 本文から部材ごとの単価・納期・納期回答日を抽出し、担当者が確認して反映する
"""
import email
import email.policy
import imaplib
import json
import re
from datetime import date, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime

from . import db, mailflow, orders, pagereader

# ------------------------------------------------------------------ 読み込み


def parse_eml(raw):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            body = part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = part.get_payload(decode=True).decode("utf-8", "replace")
        if part.get_content_type() == "text/html":
            body = pagereader.visible_text(body)
    frm = getaddresses([str(msg.get("From", ""))])
    try:
        when = parsedate_to_datetime(msg["Date"]).astimezone().strftime("%Y-%m-%d %H:%M:%S") if msg["Date"] else None
    except (TypeError, ValueError):
        when = None
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "references": str(msg.get("References") or ""),
        "from_name": frm[0][0] if frm else "", "from_addr": (frm[0][1] if frm else "").lower(),
        "subject": str(msg.get("Subject") or ""), "received_at": when, "body": body.strip(),
    }


def parse_msg(raw):
    """Outlook の .msg ファイル。"""
    import os
    import tempfile

    import extract_msg
    fd, path = tempfile.mkstemp(suffix=".msg")  # extract_msg はファイルのパスで渡すのが確実
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        m = extract_msg.Message(path)
    except Exception as e:
        os.unlink(path)
        raise ValueError("Outlook の .msg ファイルとして読めませんでした") from e
    try:
        hdr = m.header
        frm = getaddresses([m.sender or ""])
        when = m.date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(m.date, "strftime") else (str(m.date) if m.date else None)
        body = m.body or (pagereader.visible_text(m.htmlBody.decode("utf-8", "replace")) if m.htmlBody else "")
        return {
            "message_id": (hdr.get("Message-ID") if hdr else None) or m.messageId,
            "in_reply_to": hdr.get("In-Reply-To") if hdr else None,
            "references": str(hdr.get("References") or "") if hdr else "",
            "from_name": frm[0][0] if frm else "", "from_addr": (frm[0][1] if frm else "").lower(),
            "subject": m.subject or "", "received_at": when, "body": (body or "").strip(),
        }
    finally:
        m.close()
        os.unlink(path)


def parse_file(filename, raw):
    if filename.lower().endswith(".msg"):
        return parse_msg(raw)
    return parse_eml(raw)


# ------------------------------------------------------------------ 対応付け
_PREFIX = re.compile(r"^\s*((re|fw|fwd|返信|転送|ＲＥ)\s*[:：]\s*|\[[^\]]*\]\s*)+", re.I)


def norm_subject(s):
    return re.sub(r"\s+", "", _PREFIX.sub("", s or ""))


def _norm(s):
    return re.sub(r"[\s\-_/]", "", s or "").upper()


def match(conn, p):
    """(送ったメール, 方法) を返す。見つからなければ (None, None)。"""
    ids = re.findall(r"<[^>]+>", " ".join(filter(None, [p.get("in_reply_to"), p.get("references")])))
    for mid in reversed(ids):
        e = conn.execute("SELECT * FROM emails WHERE message_id=?", (mid,)).fetchone()
        if e:
            return e, "返信の宛先 (In-Reply-To)"
    subj = norm_subject(p.get("subject"))
    if len(subj) >= 6:
        for e in conn.execute("SELECT * FROM emails WHERE status='sent' AND kind!='alert' "
                              "ORDER BY sent_at DESC, id DESC LIMIT 500").fetchall():
            ns = norm_subject(e["subject"])
            if ns and (ns in subj or subj in ns):
                return e, "件名"
    sup = supplier_of(conn, p.get("from_addr"))
    if sup:
        body = _norm(p.get("body"))
        for e in conn.execute(f"SELECT * FROM emails WHERE supplier_id=? AND status='sent' AND kind IN "
                              f"{mailflow.AWAIT_REPLY + ('order',)} ORDER BY sent_at DESC, id DESC LIMIT 50",
                              (sup["id"],)).fetchall():
            if any(_norm(m["part_number"]) in body for m in mailflow.linked_materials(conn, e["id"])):
                return e, "差出人と品番"
    return None, None


def supplier_of(conn, addr):
    if not addr:
        return None
    return conn.execute("SELECT * FROM suppliers WHERE lower(email)=lower(?)", (addr,)).fetchone()


# ------------------------------------------------------------------ 抽出
PRICE = re.compile(r"(?:(?:単価|価格|金額|見積|御見積|お見積)[^\n\d¥￥]{0,12})?[¥￥]\s*([\d,]+(?:\.\d+)?)|"
                   r"(?:単価|価格|見積単価|御見積単価)[^\n\d]{0,12}([\d,]+(?:\.\d+)?)\s*円?|"
                   r"([\d,]+(?:\.\d+)?)\s*円")
LEAD_DAYS = re.compile(r"(?:納期|納入|出荷|リードタイム|LT)[^\n\d]{0,12}(?:約|受注後|ご注文後|発注後)?\s*"
                       r"(\d+)\s*(?:[~〜～\-]\s*(\d+)\s*)?(営業日|日|週間|週|ヶ月|か月|カ月|ケ月)")
LEAD_DATE = re.compile(r"(?:納期|納入|出荷|入荷|納品)[^\n\d]{0,16}(?:(20\d{2})\s*[/年\-.]\s*)?(\d{1,2})\s*[/月\-.]\s*(\d{1,2})\s*日?")
IN_STOCK = re.compile(r"在庫品|在庫あり|在庫有|即納|即日出荷|当日出荷")
UNCHANGED = re.compile(r"(変更|変わり)[はが]?(ございません|ありません|無し|なし|ない)|据え?置き|同額")
TAX_IN = re.compile(r"税込|内税")
ORDER_OK = re.compile(r"(ご)?注文(を)?(承り|承知|受け|確認)|注文請書|受注いたしました|手配いたします")


def _num(s):
    return float(s.replace(",", "")) if s else None


def _days(n, unit):
    n = int(n)
    return {"営業日": round(n * 1.4), "日": n, "週間": n * 7, "週": n * 7}.get(unit, n * 30)


def _window(body, pn, size=400):
    """品番が出てくる付近の文章 (見つからなければ None)。"""
    nb, npn = _norm(body), _norm(pn)
    if not npn or npn not in nb:
        return None
    # 正規化前の位置を探す (空白・記号を読み飛ばしながら照合)
    pat = r"[\s\-_/]*".join(re.escape(c) for c in re.sub(r"[\s\-_/]", "", pn))
    m = re.search(pat, body, re.I)
    if not m:
        return None
    return body[max(0, m.start() - 60):m.end() + size]


def extract_values(text, today=None):
    today = today or date.today()
    out = {"unit_price": None, "tax_included": False, "lead_time_days": None, "promised_date": None,
           "unchanged": bool(UNCHANGED.search(text or "")), "in_stock": bool(IN_STOCK.search(text or ""))}
    if not text:
        return out
    for m in PRICE.finditer(text):
        v = _num(m.group(1) or m.group(2) or m.group(3))
        pre = re.split(r"[、。,，;；\n]", text[max(0, m.start() - 20):m.start()])[-1]
        post = re.split(r"[、。,，;；\n]", text[m.end():m.end() + 12])[0]
        ctx = pre + m.group(0) + post  # 同じ文節の中だけで判断する
        if v and not re.search(r"送料|合計|総額|小計|消費税|手数料|以上", ctx):
            out["unit_price"], out["tax_included"] = v, bool(TAX_IN.search(ctx))
            break
    m = LEAD_DAYS.search(text)
    if m:
        out["lead_time_days"] = _days(m.group(2) or m.group(1), m.group(3))
    d = LEAD_DATE.search(text)
    if d:
        y = int(d.group(1)) if d.group(1) else today.year
        try:
            dt = date(y, int(d.group(2)), int(d.group(3)))
            if not d.group(1) and dt < today - timedelta(days=60):
                dt = date(y + 1, dt.month, dt.day)  # 年の記載が無く過去なら翌年
            out["promised_date"] = dt.isoformat()
            if out["lead_time_days"] is None and dt >= today:
                out["lead_time_days"] = (dt - today).days
        except ValueError:
            pass
    if out["lead_time_days"] is None and out["in_stock"]:
        out["lead_time_days"] = 2
    return out


def extract(conn, body, email_row, today=None):
    """送ったメールの部材ごとに、本文から値を抽出する。"""
    mats = mailflow.linked_materials(conn, email_row["id"]) if email_row else []
    items = []
    for m in mats:
        win = _window(body, m["part_number"]) or _window(body, m["name"])
        if win is None and len(mats) == 1:
            win = body  # 1 品目だけの依頼なら本文全体から探す
        vals = extract_values(win or "", today)
        em = conn.execute("SELECT order_id FROM email_materials WHERE email_id=? AND material_id=?",
                          (email_row["id"], m["id"])).fetchone()
        oid = em["order_id"] if em else None
        if oid is None and email_row["kind"] == "order":  # 注文メールへの返信 → その注文の発注
            r = conn.execute("SELECT id FROM orders WHERE email_id=? AND material_id=?",
                             (email_row["id"], m["id"])).fetchone()
            oid = r["id"] if r else None
        items.append({"material_id": m["id"], "part_number": m["part_number"], "name": m["name"],
                      "found": win is not None, "order_id": oid, **vals,
                      "snippet": re.sub(r"\s+", " ", (win or "")[:220])})
    return {"items": items, "order_accepted": bool(ORDER_OK.search(body or ""))}


# ------------------------------------------------------------------ 保存・反映
def store(conn, p, source, actor=None):
    """受信メールを保存し、対応付けと抽出まで行う。戻り値: (inbox_id, 新規か)"""
    if p.get("message_id"):
        ex = conn.execute("SELECT id FROM inbox WHERE message_id=?", (p["message_id"],)).fetchone()
        if ex:
            return ex["id"], False
    e, how = match(conn, p)
    sup = supplier_of(conn, p.get("from_addr"))
    ext = extract(conn, p.get("body"), e) if e else {"items": [], "order_accepted": False}
    iid = conn.execute(
        "INSERT INTO inbox (message_id, in_reply_to, from_addr, from_name, subject, received_at, body, source, "
        "matched_email_id, match_method, supplier_id, extracted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.get("message_id"), p.get("in_reply_to"), p.get("from_addr"), p.get("from_name"), p.get("subject"),
         p.get("received_at"), p.get("body"), source, e["id"] if e else None, how,
         (sup["id"] if sup else (e["supplier_id"] if e else None)), json.dumps(ext, ensure_ascii=False))).lastrowid
    conn.commit()
    if e and e["kind"] in mailflow.AWAIT_REPLY and not e["answered_at"]:
        # 返信が届いた = 回答あり (値の反映は担当者が確認してから)
        mailflow.mark_answered(conn, e["id"], None, actor or "受信メール取込", None,
                               f"返信メールを受信「{p.get('subject', '')[:40]}」")
    for it in ext["items"]:
        db.log_activity(conn, None, "mail_received", it["material_id"],
                        f"{p.get('from_name') or p.get('from_addr')} から返信「{p.get('subject', '')[:40]}」",
                        actor or "受信メール取込")
    return iid, True


def rematch(conn, iid, email_id):
    row = conn.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    e = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    ext = extract(conn, row["body"], e)
    conn.execute("UPDATE inbox SET matched_email_id=?, match_method='手動', extracted=? WHERE id=?",
                 (email_id, json.dumps(ext, ensure_ascii=False), iid))
    conn.commit()


def apply(conn, iid, values, actor=None, client=None):
    """確認済みの値を反映する。values: [{material_id, unit_price, lead_time_days, promised_date,
    tax_included, unchanged, order_id}]"""
    row = conn.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    e = conn.execute("SELECT * FROM emails WHERE id=?", (row["matched_email_id"],)).fetchone() \
        if row["matched_email_id"] else None
    done = []
    for v in values:
        mid = v["material_id"]
        price = v.get("unit_price")
        if price is not None and v.get("tax_included"):
            price = round(price / 1.1, 2)
        if v.get("order_id") and v.get("promised_date"):
            orders.set_eta(conn, v["order_id"], v["promised_date"], None, actor, client, "受信メールから")
            done.append(mid)
            continue
        if price is not None or v.get("lead_time_days") is not None:
            db.add_observation(conn, mid, "quote", price, v.get("lead_time_days"),
                               supplier_id=row["supplier_id"] or (e["supplier_id"] if e else None))
            db.log_activity(conn, None, "observation", mid,
                            f"受信メールから記録: 単価 {price if price is not None else '—'} / "
                            f"納期 {v.get('lead_time_days') if v.get('lead_time_days') is not None else '—'} 日",
                            actor, client)
        elif v.get("unchanged"):
            db.log_activity(conn, None, "confirmed", mid, "受信メール: 価格・納期に変更なし", actor, client)
        else:
            continue
        mailflow.record_quote_answer(conn, mid, None, actor, client)
        done.append(mid)
    conn.execute("UPDATE inbox SET status='applied', handled_by=?, handled_at=datetime('now','localtime') "
                 "WHERE id=?", (actor, iid))
    conn.commit()
    return done


# ------------------------------------------------------------------ IMAP
def imap_configured(settings):
    return bool(settings.imap_host and settings.imap_user)


def fetch_imap(conn, settings, days=14, actor=None, client_factory=None):
    """メールサーバーから直近 days 日のメールを見て、返信・仕入先からのメールだけ取り込む。
    既読/未読などメールボックスの状態は変更しない。戻り値: dict(checked, imported, matched)"""
    factory = client_factory or (imaplib.IMAP4_SSL if settings.imap_ssl else imaplib.IMAP4)
    cli = factory(settings.imap_host, settings.imap_port)
    summary = {"checked": 0, "imported": 0, "matched": 0}
    try:
        cli.login(settings.imap_user, settings.imap_password)
        cli.select(settings.imap_folder or "INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = cli.search(None, "SINCE", since)
        ids = data[0].split() if data and data[0] else []
        known = {r[0] for r in conn.execute("SELECT message_id FROM inbox WHERE message_id IS NOT NULL")}
        own = (settings.purchaser_email or "").lower()
        for num in ids[-500:]:
            typ, hdr = cli.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])")
            head = email.message_from_bytes(hdr[0][1]) if hdr and hdr[0] else None
            mid = (head.get("Message-ID") or "").strip() if head else ""
            summary["checked"] += 1
            if mid and mid in known:
                continue
            typ, msgdata = cli.fetch(num, "(BODY.PEEK[])")
            p = parse_eml(msgdata[0][1])
            if p["from_addr"] == own:
                continue
            e, _ = match(conn, p)
            if not e and not supplier_of(conn, p["from_addr"]):
                continue  # 関係の無いメールは保存しない
            _, new = store(conn, p, "imap", actor)
            if new:
                summary["imported"] += 1
                summary["matched"] += 1 if e else 0
    finally:
        try:
            cli.logout()
        except Exception:
            pass
    return summary
