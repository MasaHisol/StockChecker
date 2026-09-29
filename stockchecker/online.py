"""ネット上から一般価格・納期を自動取得するプロバイダ。

- MouserProvider   : Mouser Search API (電子部品。API キー無料)
- DigiKeyProvider  : Digi-Key Product Information API v4 (電子部品。無料登録)
- WebSearchProvider: Claude の Web 検索で通販サイト等を調べて相場を推定
                     (機構部品・鋼材・汎用品など API のない部材向け)

各 fetch() は「オファー」 dict のリストを返す:
  unit_price, lead_time_days, stock_qty, min_order_qty, currency, vendor, url
"""
import json
import urllib.parse
import logging
import re
import time
import urllib.request

from .providers import PriceProvider

log = logging.getLogger(__name__)


def _http_json(url, payload=None, headers=None, timeout=20, form=False):
    data = None
    h = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _to_number(s):
    """'¥1,234.5' や '1.234,5 €' などから数値を取り出す。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    return float(m.group().replace(",", "")) if m else None


def price_for_qty(breaks, qty):
    """[(数量, 単価), ...] から必要数量に適用される単価を返す。"""
    breaks = sorted((q, p) for q, p in breaks if q and p is not None)
    price = None
    for q, p in breaks:
        if q <= qty:
            price = p
    return price if price is not None else (breaks[0][1] if breaks else None)


def _pn_match(a, b):
    norm = lambda s: re.sub(r"[\s\-_/]", "", (s or "")).upper()
    return norm(a) == norm(b)


# ---------------------------------------------------------------- Mouser
class MouserProvider(PriceProvider):
    name = "mouser"
    URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"

    def __init__(self, api_key):
        self.api_key = api_key

    def fetch(self, material):
        res = _http_json(self.URL.format(key=self.api_key), {
            "SearchByPartRequest": {"mouserPartNumber": material["part_number"],
                                    "partSearchOptions": "Exact"}})
        errs = res.get("Errors") or []
        if errs:
            raise RuntimeError(f"Mouser: {errs}")
        return [o for o in (self.parse_part(p, material)
                            for p in (res.get("SearchResults") or {}).get("Parts") or []) if o]

    @staticmethod
    def parse_part(p, material):
        if not _pn_match(p.get("ManufacturerPartNumber"), material["part_number"]):
            return None
        breaks = [(int(b.get("Quantity") or 0), _to_number(b.get("Price")))
                  for b in p.get("PriceBreaks") or []]
        currency = next((b.get("Currency") for b in p.get("PriceBreaks") or []), "JPY")
        stock = p.get("AvailabilityInStock")
        stock = int(stock) if stock not in (None, "") else int(_to_number(p.get("Availability")) or 0)
        lead = _to_number(p.get("LeadTime"))  # 例: "84 Days"
        return {
            "unit_price": price_for_qty(breaks, material["quantity"] or 1),
            # 在庫で足りれば数日で入荷、不足ならメーカー納期
            "lead_time_days": 5 if stock >= (material["quantity"] or 1) else (int(lead) if lead else None),
            "stock_qty": stock,
            "min_order_qty": _to_number(p.get("Min")) and int(_to_number(p.get("Min"))),
            "currency": currency or "JPY",
            "vendor": "Mouser",
            "url": p.get("ProductDetailUrl"),
        }


# ---------------------------------------------------------------- Digi-Key
class DigiKeyProvider(PriceProvider):
    name = "digikey"
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires = 0

    def _auth(self):
        if not self._token or time.time() > self._expires - 60:
            r = _http_json(self.TOKEN_URL, {"client_id": self.client_id,
                                            "client_secret": self.client_secret,
                                            "grant_type": "client_credentials"}, form=True)
            self._token = r["access_token"]
            self._expires = time.time() + int(r.get("expires_in", 600))
        return self._token

    def fetch(self, material):
        res = _http_json(self.SEARCH_URL, {"Keywords": material["part_number"], "Limit": 5},
                         headers={"Authorization": f"Bearer {self._auth()}",
                                  "X-DIGIKEY-Client-Id": self.client_id,
                                  "X-DIGIKEY-Locale-Site": "JP",
                                  "X-DIGIKEY-Locale-Language": "ja",
                                  "X-DIGIKEY-Locale-Currency": "JPY"})
        products = list(res.get("ExactMatches") or []) + list(res.get("Products") or [])
        return [o for o in (self.parse_product(p, material) for p in products) if o][:1]

    @staticmethod
    def parse_product(p, material):
        if not _pn_match(p.get("ManufacturerProductNumber"), material["part_number"]):
            return None
        qty = material["quantity"] or 1
        breaks = []
        for v in p.get("ProductVariations") or []:
            breaks += [(b.get("BreakQuantity"), b.get("UnitPrice"))
                       for b in v.get("StandardPricing") or []]
        price = price_for_qty(breaks, qty) if breaks else p.get("UnitPrice")
        stock = int(p.get("QuantityAvailable") or 0)
        weeks = _to_number(p.get("ManufacturerLeadWeeks"))
        return {
            "unit_price": price,
            "lead_time_days": 5 if stock >= qty else (int(weeks * 7) if weeks else None),
            "stock_qty": stock,
            "currency": "JPY",
            "vendor": "Digi-Key",
            "url": p.get("ProductUrl"),
        }


# ---------------------------------------------------------------- Web 検索 (Claude)
WEB_PROMPT = """あなたは日本の製造業の購買担当アシスタントです。
次の部材について、Web 検索で国内の通販サイト・商社・メーカーの公開情報を調べ、
現在の一般的な販売価格 (税抜・円/単位) と一般的な納期を調べてください。

品番: {part_number}
品名: {name}
メーカー: {maker}
仕様: {spec}
必要数量: {quantity} {unit}

ルール:
- 品番・仕様が一致する (または明確に同等の) 商品のみ採用する。推測で価格を作らない
- 税込表示なら税抜に換算 (÷1.1)。外貨なら換算して円にし、note に換算レートを書く
- 数量割引がある場合は必要数量に適用される単価
- 納期は「在庫あり→出荷までの日数」または「受注生産→日数」を日数に換算
- 見つからない項目は null

最後に、次の形式の JSON だけをコードブロックなしで出力してください:
{{"offers": [{{"vendor": "販売元", "url": "商品ページURL", "unit_price": 数値または null,
  "lead_time_days": 整数または null, "stock_qty": 整数または null, "note": "補足"}}],
  "summary": "相場の要約 (1-2文)"}}"""


class WebSearchProvider(PriceProvider):
    """Claude API の Web 検索ツールで相場を調べる。API キー (ANTHROPIC_API_KEY) が必要。"""
    name = "web"

    def __init__(self, api_key=None, model="claude-opus-5-5", max_searches=5, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_searches = max_searches

    def fetch(self, material):
        prompt = WEB_PROMPT.format(
            part_number=material["part_number"], name=material["name"],
            maker=material["maker"] or "不明", spec=material["spec"] or "-",
            quantity=material["quantity"] or 1, unit=material["unit"] or "個")
        messages = [{"role": "user", "content": prompt}]
        for _ in range(4):  # pause_turn (長い検索の中断) を再開する
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                tools=[{"type": "web_search_20260209", "name": "web_search",
                        "max_uses": self.max_searches,
                        "user_location": {"type": "approximate", "country": "JP"}}],
                messages=messages,
            )
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "refusal":
            raise RuntimeError("Web 検索がモデルに拒否されました")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return self.parse(text, material)

    @staticmethod
    def parse(text, material):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"Web 検索結果を解釈できません: {text[:200]}")
        data = json.loads(m.group())
        out = []
        for o in data.get("offers") or []:
            price = _to_number(o.get("unit_price"))
            if price is None and o.get("lead_time_days") is None:
                continue
            out.append({
                "unit_price": price,
                "lead_time_days": int(o["lead_time_days"]) if o.get("lead_time_days") is not None else None,
                "stock_qty": int(o["stock_qty"]) if o.get("stock_qty") is not None else None,
                "currency": "JPY",
                "vendor": o.get("vendor") or "Web",
                "url": o.get("url"),
                "note": o.get("note"),
            })
        return out


def pick_best(offers, quantity):
    """複数オファーから代表値を選ぶ: 必要数の在庫がある中で最安、なければ全体で最安。
    納期は採用オファーのもの (不明なら全オファーの最短)。"""
    priced = [o for o in offers if o.get("unit_price") is not None]
    if not priced:
        leads = [o["lead_time_days"] for o in offers if o.get("lead_time_days") is not None]
        return {"lead_time_days": min(leads)} if leads else None
    stocked = [o for o in priced if (o.get("stock_qty") or 0) >= quantity]
    best = dict(min(stocked or priced, key=lambda o: o["unit_price"]))
    if best.get("lead_time_days") is None:
        leads = [o["lead_time_days"] for o in offers if o.get("lead_time_days") is not None]
        best["lead_time_days"] = min(leads) if leads else None
    return best
