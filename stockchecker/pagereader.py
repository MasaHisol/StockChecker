"""商品ページ URL から価格・納期・在庫・商品名を読み取る。

読み取りは次の順に試す:
  1. ページに埋め込まれた商品データ (schema.org JSON-LD / microdata / OGP)
  2. ページ内スクリプトの JSON に含まれる price 系の項目
  3. サイトごとに記憶した「価格の見出し」(利用者が候補から一度選ぶと保存される)
  4. 「価格」「販売価格」などの見出しの近くにある金額 (推定)
静的な HTML で価格が見つからない場合は、PC の Edge / Chrome をバックグラウンドで使って
JavaScript 実行後のページを読み直す。
"""
import html as htmlmod
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .online import _to_number

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

PRICE_RE = re.compile(r"(?:[¥￥]\s*([\d,]+(?:\.\d+)?))|(?:([\d,]+(?:\.\d+)?)\s*円)")
GOOD_LABEL = re.compile(r"価格|単価|販売|通常|特価|本体|税抜|税別|price|Price")
BAD_LABEL = re.compile(r"送料|ポイント|合計|小計|以上|割引|OFF|クーポン|手数料|最大|まで|定価|希望小売|参考")
LEAD_PATTERNS = [
    (re.compile(r"(\d+)\s*[~〜～\-]\s*(\d+)\s*営業日"), 1.4),
    (re.compile(r"(\d+)\s*営業日"), 1.4),
    (re.compile(r"当日\s*(?:出荷|発送)"), None),
    (re.compile(r"翌日\s*(?:出荷|発送|お届け)"), None),
    (re.compile(r"(\d+)\s*日[^\d\n]{0,6}(?:出荷|発送|お届け)"), 1.0),
    (re.compile(r"(\d+)\s*週間"), 7.0),
]
STOCK_OUT = re.compile(r"在庫切れ|在庫なし|欠品|取り寄せ|お取寄せ|販売終了|入荷待ち")
STOCK_IN = re.compile(r"在庫あり|在庫有|即納|当日出荷")


@dataclass
class PageInfo:
    url: str
    unit_price: float = None
    lead_time_days: int = None
    stock_qty: int = None
    in_stock: bool = None
    title: str = None
    part_number: str = None
    maker: str = None
    method: str = None              # どの方法で価格を読めたか
    tax_included: bool = None
    candidates: list = field(default_factory=list)   # 価格候補 [{value, label, context, tax}]
    rendered: bool = False


class FetchError(Exception):
    pass


# ------------------------------------------------------------------ 取得
def fetch_static(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            charset = r.headers.get_content_charset()
    except urllib.error.HTTPError as e:
        raise FetchError(f"サイトが HTTP {e.code} を返しました") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"サイトに接続できません ({getattr(e, 'reason', e)})") from e
    if not charset:
        m = re.search(rb'charset=["\']?([\w\-]+)', raw[:4000])
        charset = m.group(1).decode() if m else "utf-8"
    return raw.decode(charset, errors="replace")


def fetch_rendered(url, timeout_ms=30000):
    """PC にある Edge / Chrome (無ければ Playwright の Chromium) で表示した HTML を返す。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError("ブラウザ表示機能が利用できません") from e
    import os
    last = None
    exe = os.environ.get("SC_BROWSER_PATH")  # 任意: 使うブラウザの実行ファイルを明示
    with sync_playwright() as p:
        for channel in (["exe"] if exe else []) + ["msedge", "chrome", None]:
            try:
                if channel == "exe":
                    b = p.chromium.launch(executable_path=exe, headless=True)
                elif channel:
                    b = p.chromium.launch(channel=channel, headless=True)
                else:
                    b = p.chromium.launch(headless=True)
            except Exception as e:
                last = e
                continue
            try:
                ctx = b.new_context(locale="ja-JP", user_agent=HEADERS["User-Agent"])
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                return page.content()
            finally:
                b.close()
    raise FetchError(f"Edge / Chrome を起動できませんでした ({last})")


# ------------------------------------------------------------------ 解析
def _iter_jsonld(text):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(htmlmod.unescape(m.group(1).strip()))
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


def _types(d):
    t = d.get("@type")
    return set(t) if isinstance(t, list) else {t}


def visible_text(html):
    t = re.sub(r"<head[^>]*>.*?</head>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<(script|style|noscript|svg|title)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(p|div|li|tr|td|th|dt|dd|h\d|span|label)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmlmod.unescape(t)
    t = re.sub(r"[ \t　\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _tax_of(ctx):
    if re.search(r"税込|税抜き?前|内税", ctx):
        return "incl"
    if re.search(r"税抜|税別|本体価格|外税", ctx):
        return "excl"
    return None


def price_candidates(text, limit=40):
    out, seen = [], set()
    for m in PRICE_RE.finditer(text):
        v = _to_number(m.group(1) or m.group(2))
        if not v or v <= 0:
            continue
        before = text[max(0, m.start() - 40):m.start()]
        line = before.split("\n")[-1].strip() or before.strip().split("\n")[-1].strip()
        toks = line.split(" ")
        # 見出しは直前の語 (商品ごとに変わる文字列を含めないため)
        label = toks[-1] if len(toks[-1]) >= 2 or len(toks) == 1 else " ".join(toks[-2:])
        label = label[-20:]
        after = text[m.end():m.end() + 16].split("\n")[0]
        key = (v, label)
        if key in seen:
            continue
        seen.add(key)
        ctx = f"{label} {m.group(0)}{after}"
        out.append({"value": v, "label": label.strip(), "context": ctx.strip(),
                    "tax": _tax_of(label + after)})
        if len(out) >= limit:
            break
    return out


def _price_after_label(text, label):
    i = text.find(label)
    while i >= 0:
        m = PRICE_RE.search(text, i + len(label), i + len(label) + 80)
        if m:
            return _to_number(m.group(1) or m.group(2)), _tax_of(text[i:m.end() + 16])
        i = text.find(label, i + 1)
    return None, None


EMBED_KEYS = r"(?:price|salePrice|salesPrice|sellingPrice|unitPrice|standardUnitPrice|priceExcludingTax|itemPrice)"


def parse(html, url="", hint=None):
    info = PageInfo(url=url)
    # 1. JSON-LD
    for d in _iter_jsonld(html):
        ts = _types(d)
        if "Product" in ts:
            info.title = info.title or d.get("name")
            info.part_number = info.part_number or d.get("mpn") or d.get("sku")
            brand = d.get("brand")
            info.maker = info.maker or (brand.get("name") if isinstance(brand, dict) else brand)
        if ts & {"Offer", "AggregateOffer"} and info.unit_price is None:
            p = _to_number(d.get("price") or d.get("lowPrice"))
            if p:
                info.unit_price, info.method = p, "商品データ (JSON-LD)"
            avail = str(d.get("availability") or "")
            if avail:
                info.in_stock = "InStock" in avail
            inv = d.get("inventoryLevel")
            if isinstance(inv, dict) and _to_number(inv.get("value")) is not None:
                info.stock_qty = int(_to_number(inv.get("value")))
            ship = d.get("deliveryLeadTime") or (d.get("shippingDetails") or {}).get("deliveryTime")
            if isinstance(ship, dict):
                v = ship.get("maxValue") or ship.get("value") or \
                    (ship.get("handlingTime") or {}).get("maxValue")
                if _to_number(v) is not None:
                    info.lead_time_days = int(_to_number(v))
    # microdata / OGP
    if info.unit_price is None:
        m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)', html) or \
            re.search(r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']', html) or \
            re.search(r'property=["\'](?:product|og):price:amount["\'][^>]*content=["\']([^"\']+)', html)
        if m and _to_number(m.group(1)):
            info.unit_price, info.method = _to_number(m.group(1)), "商品データ (meta)"
    if not info.title:
        m = re.search(r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)', html) or \
            re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            info.title = htmlmod.unescape(m.group(1)).strip()[:120]

    text = visible_text(html)
    info.candidates = price_candidates(text)
    # 3. 記憶した見出し
    if hint and hint.get("label"):
        v, tax = _price_after_label(text, hint["label"])
        if v:
            info.unit_price, info.method = v, f"記憶した見出し「{hint['label']}」"
            info.tax_included = (hint.get("tax") == "incl") if hint.get("tax") else tax == "incl"
    # 2. 埋め込み JSON
    if info.unit_price is None:
        m = re.search(r'"' + EMBED_KEYS + r'"\s*:\s*"?([\d.,]+)', html)
        if m and _to_number(m.group(1)):
            info.unit_price, info.method = _to_number(m.group(1)), "ページ内データ"
    # 4. 見出しからの推定
    if info.unit_price is None:
        for c in info.candidates:
            if GOOD_LABEL.search(c["label"]) and not BAD_LABEL.search(c["context"]):
                info.unit_price, info.method = c["value"], f"推定 (「{c['label']}」の金額)"
                info.tax_included = c["tax"] == "incl"
                break
    # 在庫・納期
    if info.in_stock is None:
        if STOCK_OUT.search(text):
            info.in_stock = False
        elif STOCK_IN.search(text):
            info.in_stock = True
    if info.lead_time_days is None:
        for pat, mult in LEAD_PATTERNS:
            m = pat.search(text)
            if m:
                if mult is None:
                    info.lead_time_days = 1 if "当日" in m.group(0) else 2
                else:
                    info.lead_time_days = round(int(m.groups()[-1]) * mult)
                break
    return info


def read(url, hint=None, allow_render=True):
    """URL を読み取り PageInfo を返す。価格が見つからなければブラウザ表示で再挑戦する。"""
    if not re.match(r"https?://", url or ""):
        raise FetchError("URL は http:// または https:// で始まる必要があります")
    err = None
    try:
        info = parse(fetch_static(url), url, hint)
        if info.unit_price is not None:
            return info
    except FetchError as e:
        err, info = e, None
    if allow_render:
        try:
            r = parse(fetch_rendered(url), url, hint)
            r.rendered = True
            if r.unit_price is not None or info is None or len(r.candidates) > len(info.candidates):
                return r
        except FetchError as e:
            if info is None:
                raise FetchError(f"{err}。ブラウザでの表示も失敗しました: {e}") from e
    if info is None:
        raise err
    return info


def net_price(info):
    """税込と分かっている場合は税抜に換算した単価。"""
    if info.unit_price is None:
        return None
    return round(info.unit_price / 1.1, 1) if info.tax_included else info.unit_price


def domain(url):
    return urllib.parse.urlparse(url).netloc.lower()
