"""見積依頼・注文メールの文面生成と送信。"""
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def _sign(settings):
    return (f"\n--\n{settings.company_name}\n{settings.purchaser_name}\n"
            f"{settings.purchaser_email}\n")


def _item_block(material, quantity=None):
    lines = [
        f"  品名　　: {material['name']}",
        f"  品番　　: {material['part_number']}",
    ]
    if material["maker"]:
        lines.append(f"  メーカー: {material['maker']}")
    if material["spec"]:
        lines.append(f"  仕様　　: {material['spec']}")
    lines.append(f"  数量　　: {quantity or material['quantity']} {material['unit']}")
    if material["required_date"]:
        lines.append(f"  希望納期: {material['required_date']}")
    return "\n".join(lines)


def build_rfq(material, supplier, settings, reasons=()):
    subject = f"【見積依頼】{material['name']} ({material['part_number']}) - {settings.company_name}"
    contact = supplier["contact_name"] or "ご担当者"
    body = (
        f"{supplier['name']}\n{contact} 様\n\n"
        f"いつもお世話になっております。{settings.company_name}の{settings.purchaser_name}です。\n\n"
        "下記部材につきまして、お見積りをお願いいたします。\n\n"
        f"{_item_block(material)}\n\n"
        "お手数ですが、以下をご回答いただけますと幸いです。\n"
        "  ・単価 (税抜) および見積有効期限\n"
        "  ・納期 (受注後の日数、または最短入荷日)\n"
        "  ・最小発注数量 / 在庫状況\n\n"
        "ご多用のところ恐縮ですが、よろしくお願いいたします。\n"
        + _sign(settings)
    )
    return subject, body


def build_order(material, supplier, settings, quantity, unit_price, delivery_date):
    amount = quantity * unit_price
    subject = f"【注文書】{material['name']} ({material['part_number']}) - {settings.company_name}"
    contact = supplier["contact_name"] or "ご担当者"
    body = (
        f"{supplier['name']}\n{contact} 様\n\n"
        f"いつもお世話になっております。{settings.company_name}の{settings.purchaser_name}です。\n\n"
        "お見積りいただきました下記部材につきまして、以下のとおり注文いたします。\n\n"
        f"{_item_block(material, quantity)}\n"
        f"  単価　　: {unit_price:,.0f} 円 (税抜)\n"
        f"  金額　　: {amount:,.0f} 円 (税抜)\n"
        f"  納期　　: {delivery_date}\n\n"
        "恐れ入りますが、注文請書または受領のご連絡をお願いいたします。\n"
        + _sign(settings)
    )
    return subject, body


def build_alert(owner, items, settings):
    """担当者向けアラートメール。items は (material, [Finding]) のリスト。"""
    subject = f"【部材アラート】見積取得・確認が必要な部材 {len(items)} 件"
    parts = [f"{owner['name']} さん\n\n以下の部材で対応が必要です。\n"]
    for m, findings in items:
        parts.append(f"■ {m['name']} ({m['part_number']})")
        for f in findings:
            mark = "[要見積]" if f.needs_quote else "[注意]"
            parts.append(f"  {mark} {f.message}")
        parts.append("")
    parts.append("見積依頼メールの下書きはアプリの「メール」画面から確認・送信できます。")
    return subject, "\n".join(parts) + _sign(settings)


def to_message(email_row, settings):
    msg = EmailMessage()
    msg["From"] = f"{settings.purchaser_name} <{settings.purchaser_email}>"
    msg["To"] = email_row["to_addr"]
    if email_row["cc_addr"]:
        msg["Cc"] = email_row["cc_addr"]
    msg["Subject"] = email_row["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(email_row["body"])
    return msg


def deliver(msg, settings):
    """SMTP 設定があれば送信、なければ outbox に .eml として保存する。
    戻り値は送信先の説明 (ログ用)。"""
    if settings.smtp_host:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return f"smtp://{settings.smtp_host}"
    out = Path(settings.outbox_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y%m%d-%H%M%S-%f}.eml"
    path.write_bytes(bytes(msg))
    return str(path)
