"""部材ごとに「見積取得が必要か」「担当者に知らせるべきリスクがあるか」を判定する。

副作用のない純粋関数にしてあるので、しきい値の調整やテストが容易。
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    needs_quote: bool   # True: 見積取得が必要 / False: 注意喚起のみ


def _parse_date(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s)[:19].replace(" ", "T")).date() \
        if len(str(s)) > 10 else date.fromisoformat(str(s))


def evaluate(material, latest, previous, rules, today=None):
    """material / latest / previous は dict 互換 (sqlite3.Row 可)。"""
    today = today or date.today()
    out = []
    qty = material["quantity"] or 1

    if material["custom_item"]:
        out.append(Finding("custom_item", "特注品のため都度見積が必要です。", True))

    if latest is None or latest["unit_price"] is None:
        out.append(Finding("no_price", "一般価格の情報がありません。見積を取得してください。", True))
    else:
        price = latest["unit_price"]
        observed = _parse_date(latest["observed_at"])
        age = (today - observed).days if observed else None
        if age is not None and age > rules.stale_days:
            out.append(Finding(
                "stale_price",
                f"価格情報が {age} 日前のものです (基準 {rules.stale_days} 日)。最新見積を取得してください。",
                True))
        if previous is not None and previous["unit_price"]:
            pct = (price - previous["unit_price"]) / previous["unit_price"] * 100
            if abs(pct) >= rules.price_change_pct:
                direction = "上昇" if pct > 0 else "下落"
                out.append(Finding(
                    "price_change",
                    f"単価が前回 {previous['unit_price']:,.0f} → {price:,.0f} 円 ({pct:+.1f}%) に{direction}しています。",
                    True))
        budget = material["budget_unit_price"]
        if budget and price > budget:
            out.append(Finding(
                "over_budget",
                f"一般単価 {price:,.0f} 円が予算単価 {budget:,.0f} 円を超過しています。",
                True))
        amount = price * qty
        if amount >= rules.high_value_amount:
            out.append(Finding(
                "high_value",
                f"概算金額 {amount:,.0f} 円が基準 {rules.high_value_amount:,.0f} 円以上のため、正式見積が必要です。",
                True))

    if latest is not None:
        lt = latest["lead_time_days"]
        required = _parse_date(material["required_date"])
        if lt is not None and required is not None:
            eta = today + timedelta(days=lt)
            if eta > required:
                out.append(Finding(
                    "lead_time_over",
                    f"一般納期 {lt} 日では入荷予定 {eta} となり、必要納期 {required} に間に合いません。",
                    True))
            elif (required - eta).days < rules.lead_time_margin_days:
                out.append(Finding(
                    "lead_time_tight",
                    f"入荷予定 {eta} と必要納期 {required} の余裕が {(required - eta).days} 日しかありません。早めの発注を推奨します。",
                    False))
        stock = latest["stock_qty"]
        if stock is not None and stock < qty:
            out.append(Finding(
                "stock_short",
                f"流通在庫 {stock} が必要数 {qty} に不足しています。",
                True))
    return out


def needs_quote(findings):
    return any(f.needs_quote for f in findings)
