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
from . import pagereader
from .providers import PriceProvider

UA = "Mozilla/5.0 (StockChecker price watcher)"


def _norm(s):
    return re.sub(r"[\s\-_/]", "", s or "").upper()


def name_contains_pn(name, pn):
    """商品名に品番が含まれるか (記号・空白の違いは無視)。"""
    return bool(pn) and _norm(pn) in _norm(name)


# ------------------------------------------------------------ ページ監視
def parse_product_page(text):
    """互換用: HTML から価格等を抽出する。"""
    i = pagereader.parse(text)
    return {"unit_price": i.unit_price, "stock_qty": i.stock_qty,
            "lead_time_days": i.lead_time_days, "in_stock": i.in_stock}


def load_hints(db_path):
    if not db_path:
        return {}
    from . import db
    conn = db.connect(db_path)
    try:
        return {r["domain"]: dict(r) for r in conn.execute("SELECT * FROM site_hints")}
    finally:
        conn.close()


def info_to_offer(info, quantity):
    stock = info.stock_qty
    if stock is None and info.in_stock:
        stock = quantity  # 「在庫あり」表示のみ → 必要数ありとみなす
    elif stock is None and info.in_stock is False:
        stock = 0
    return {"unit_price": pagereader.net_price(info), "stock_qty": stock,
            "lead_time_days": info.lead_time_days, "currency": "JPY",
            "vendor": pagereader.domain(info.url), "url": info.url,
            "note": info.method}


class PageWatchProvider(PriceProvider):
    name = "page"

    def __init__(self, db_path=None, allow_render=True):
        self.db_path = db_path
        self.allow_render = allow_render

    def fetch(self, material):
        urls = [u.strip() for u in (material["watch_urls"] or "").splitlines() if u.strip()]
        if not urls:
            return []
        hints = load_hints(self.db_path)
        offers = []
        for url in urls:
            info = pagereader.read(url, hints.get(pagereader.domain(url)), self.allow_render)
            if info.unit_price is None and info.lead_time_days is None:
                raise ValueError(f"価格を読み取れませんでした: {url} (部材画面の「URL を貼り付けて追跡」で価格の場所を指定してください)")
            offers.append(info_to_offer(info, material["quantity"] or 1))
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
