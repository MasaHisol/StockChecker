"""AI を使わない無料の取得元。

- PageWatchProvider   : 部材ごとに登録した商品ページ URL を定期巡回し、ページに埋め込まれた
                        価格 (schema.org JSON-LD / microdata / OGP メタタグ) を読む。キー不要
- YahooShoppingProvider: Yahoo!ショッピング 商品検索 API v3 (無料の Client ID が必要)
- RakutenProvider     : 楽天市場 商品検索 API (無料のアプリ ID が必要)
"""
import html
import json
import re
import urllib.parse
import urllib.request

from .online import _http_json, _pn_match, _to_number
from .providers import PriceProvider

UA = "Mozilla/5.0 (StockChecker price watcher)"


def _norm(s):
    return re.sub(r"[\s\-_/]", "", s or "").upper()


def name_contains_pn(name, pn):
    """商品名に品番が含まれるか (記号・空白の違いは無視)。"""
    return bool(pn) and _norm(pn) in _norm(name)


# ------------------------------------------------------------ ページ監視
LEAD_PATTERNS = [
    (re.compile(r"(\d+)\s*[~〜～-]\s*(\d+)\s*営業日"), 1.4),
    (re.compile(r"(\d+)\s*営業日"), 1.4),
    (re.compile(r"(\d+)\s*日[^\d]{0,6}(?:出荷|発送|お届け)"), 1.0),
    (re.compile(r"(\d+)\s*週間"), 7.0),
]


def _iter_jsonld(text):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(html.unescape(m.group(1).strip()))
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                yield d
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))


def parse_product_page(text):
    """HTML から価格・在庫・納期を抽出する。見つからない項目は None。"""
    out = {"unit_price": None, "stock_qty": None, "lead_time_days": None, "in_stock": None}
    for d in _iter_jsonld(text):
        t = d.get("@type")
        if (t == "Offer" or t == "AggregateOffer" or (isinstance(t, list) and "Offer" in t)) \
                and out["unit_price"] is None:
            out["unit_price"] = _to_number(d.get("price") or d.get("lowPrice"))
            avail = str(d.get("availability") or "")
            if avail:
                out["in_stock"] = "InStock" in avail
            inv = d.get("inventoryLevel")
            if isinstance(inv, dict):
                out["stock_qty"] = _to_number(inv.get("value"))
            ship = d.get("deliveryLeadTime") or (d.get("shippingDetails") or {}).get("deliveryTime")
            if isinstance(ship, dict):
                v = ship.get("maxValue") or ship.get("value") or \
                    (ship.get("handlingTime") or {}).get("maxValue")
                if v is not None:
                    out["lead_time_days"] = int(_to_number(v))
    if out["unit_price"] is None:
        m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)', text) or \
            re.search(r'property=["\'](?:product|og):price:amount["\'][^>]*content=["\']([^"\']+)', text)
        if m:
            out["unit_price"] = _to_number(m.group(1))
    if out["lead_time_days"] is None:
        plain = re.sub(r"<[^>]+>", " ", text)
        for pat, mult in LEAD_PATTERNS:
            m = pat.search(plain)
            if m:
                out["lead_time_days"] = round(int(m.groups()[-1]) * mult)
                break
    if out["stock_qty"] is not None:
        out["stock_qty"] = int(out["stock_qty"])
    return out


class PageWatchProvider(PriceProvider):
    name = "page"

    def __init__(self, timeout=20):
        self.timeout = timeout

    def fetch(self, material):
        urls = [u.strip() for u in (material["watch_urls"] or "").splitlines() if u.strip()]
        offers = []
        for url in urls:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "ja,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                text = r.read().decode(charset, errors="replace")
            info = parse_product_page(text)
            if info["unit_price"] is None and info["lead_time_days"] is None:
                raise ValueError(f"価格を読み取れませんでした: {url}")
            qty = material["quantity"] or 1
            stock = info["stock_qty"]
            if stock is None and info["in_stock"]:
                stock = qty  # 「在庫あり」表示のみ → 必要数ありとみなす
            elif stock is None and info["in_stock"] is False:
                stock = 0
            offers.append({"unit_price": info["unit_price"], "stock_qty": stock,
                           "lead_time_days": info["lead_time_days"], "currency": "JPY",
                           "vendor": urllib.parse.urlparse(url).netloc, "url": url})
        return offers


# ------------------------------------------------------------ Yahoo!ショッピング
class YahooShoppingProvider(PriceProvider):
    name = "yahoo"
    URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"

    def __init__(self, app_id):
        self.app_id = app_id

    def fetch(self, material):
        q = urllib.parse.urlencode({"appid": self.app_id, "query": material["part_number"],
                                    "results": 20, "in_stock": "true", "sort": "+price"})
        res = _http_json(f"{self.URL}?{q}")
        return self.parse(res, material)

    @staticmethod
    def parse(res, material):
        out = []
        for h in res.get("hits") or []:
            if not name_contains_pn(h.get("name"), material["part_number"]):
                continue
            price = _to_number(h.get("price"))
            if price:
                price = round(price / 1.1, 1)  # Yahoo は税込表示 → 税抜
            out.append({"unit_price": price, "stock_qty": None, "lead_time_days": None,
                        "currency": "JPY", "url": h.get("url"),
                        "vendor": f"Yahoo!/{(h.get('seller') or {}).get('name', '')}".rstrip("/")})
        return out[:5]


# ------------------------------------------------------------ 楽天市場
class RakutenProvider(PriceProvider):
    name = "rakuten"
    URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"

    def __init__(self, app_id, url=None):
        self.app_id = app_id
        self.url = url or self.URL

    def fetch(self, material):
        q = urllib.parse.urlencode({"applicationId": self.app_id, "format": "json",
                                    "keyword": material["part_number"], "availability": 1,
                                    "sort": "+itemPrice", "hits": 20})
        return self.parse(_http_json(f"{self.url}?{q}"), material)

    @staticmethod
    def parse(res, material):
        out = []
        for it in res.get("Items") or []:
            it = it.get("Item", it)
            if not name_contains_pn(it.get("itemName"), material["part_number"]):
                continue
            price = _to_number(it.get("itemPrice"))
            if price and it.get("taxFlag", 0) == 0:
                price = round(price / 1.1, 1)  # 税込 → 税抜
            out.append({"unit_price": price, "stock_qty": None, "lead_time_days": None,
                        "currency": "JPY", "url": it.get("itemUrl"),
                        "vendor": f"楽天/{it.get('shopName', '')}"})
        return out[:5]
