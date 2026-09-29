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
BAD_LABEL = re.compile(r"送料|配送料|ポイント|合計|小計|以上|割引|OFF|クーポン|手数料|最大|まで|定価|希望小売|参考|基準|～")
LEAD_PATTERNS = [
    (re.compile(r"(\d+)\s*[~〜～\-]\s*(\d+)\s*営業日"), 1.4),
    (re.compile(r"(\d+)\s*営業日"), 1.4),
    (re.compile(r"当日\s*(?:出荷|発送)"), None),
    (re.compile(r"翌日\s*(?:出荷|発送|お届け)"), None),
    (re.compile(r"出荷日[^\d\n]{0,10}(\d+)\s*日目"), 1.0),
    (re.compile(r"(\d+)\s*日目\s*(?:出荷|発送)"), 1.0),
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
    is_group: bool = False          # サイズ違いをまとめた一覧ページ
    html: str = None                # 読み取った HTML (トラブル調査用)


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


PRICE_JS = r"""() => /[¥￥]\s?[0-9]|[0-9,]+\s?円/.test(document.body ? document.body.innerText : "")"""


def fetch_rendered(url, timeout_ms=30000, headless=True):
    """PC にある Edge / Chrome (無ければ Playwright の Chromium) で表示した HTML を返す。

    headless=False のときは画面外にウィンドウを出して通常のブラウザとして開く
    (自動アクセス対策で画面なしブラウザを拒否するサイト向け)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError("ブラウザ表示機能が利用できません") from e
    import os
    last = None
    exe = os.environ.get("SC_BROWSER_PATH")  # 任意: 使うブラウザの実行ファイルを明示
    args = ["--disable-blink-features=AutomationControlled"]
    if not headless:
        args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    with sync_playwright() as p:
        for channel in (["exe"] if exe else []) + ["msedge", "chrome", None]:
            opts = {"headless": headless, "args": args}
            try:
                if channel == "exe":
                    b = p.chromium.launch(executable_path=exe, **opts)
                elif channel:
                    b = p.chromium.launch(channel=channel, **opts)
                else:
                    b = p.chromium.launch(**opts)
            except Exception as e:
                last = e
                continue
            try:
                ctx_opts = {"locale": "ja-JP", "viewport": {"width": 1280, "height": 900}}
                if headless:  # 画面なしブラウザの識別文字列を通常のものに置き換える
                    ctx_opts["user_agent"] = HEADERS["User-Agent"]
                ctx = b.new_context(**ctx_opts)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                try:  # 価格が後から読み込まれるページは金額が表示されるまで待つ
                    page.wait_for_function(PRICE_JS, timeout=15000)
                    page.wait_for_timeout(1500)
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


def _cells(row_html):
    return [re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S | re.I)]


def table_prices(html):
    """表形式の価格 (見出し行の「単価」列の値) を探す。[(金額, 見出し)]"""
    out = []
    html = re.sub(r"<(script|style|template|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        rows = [_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)]
        rows = [r for r in rows if r]
        for hi, head in enumerate(rows[:-1]):
            for ci, h in enumerate(head):
                if GOOD_LABEL.search(h) and not BAD_LABEL.search(h) and len(h) <= 20:
                    for row in rows[hi + 1:hi + 2]:
                        if ci < len(row):
                            m = PRICE_RE.search(row[ci]) or re.fullmatch(r"([\d,]+(?:\.\d+)?)", row[ci])
                            if m:
                                v = _to_number(m.group(0))
                                if v:
                                    out.append((v, h))
    return out


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
    if url and not info.part_number:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        for key in ("HissuCode", "hissuCode", "partNumber", "pn", "model"):
            if q.get(key):
                info.part_number = q[key][0].strip()
                break
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
        if not v:
            v = next((tv for tv, th in table_prices(html) if th == hint["label"]), None)
        if v:
            info.unit_price, info.method = v, f"記憶した見出し「{hint['label']}」"
            info.tax_included = (hint.get("tax") == "incl") if hint.get("tax") else tax == "incl"
    # 表の「単価」列
    if info.unit_price is None:
        tp = table_prices(html)
        if tp:
            v, h = tp[0]
            info.unit_price, info.method = v, f"表の「{h}」列"
            info.tax_included = _tax_of(h) == "incl"
            if not any(c["value"] == v for c in info.candidates):
                info.candidates.insert(0, {"value": v, "label": h, "context": f"{h} ¥{v:,.0f}", "tax": _tax_of(h)})
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
    # 税込/税別の補正: 読んだ価格がページ上の「税込」表示と一致し、
    # 近い値の「販売価格(税別)」表示があれば、そちらを採用する (例: モノタロウ)
    if info.unit_price is not None and not (info.method or "").startswith("記憶"):
        same = [c for c in info.candidates if abs(c["value"] - info.unit_price) < 0.01]
        if any(c["tax"] == "incl" for c in same) and not any(c["tax"] == "excl" for c in same):
            excl = [c for c in info.candidates if c["tax"] == "excl" and GOOD_LABEL.search(c["label"])
                    and not BAD_LABEL.search(c["label"])
                    and 0.88 <= c["value"] / info.unit_price * 1.1 <= 1.02]
            if excl:
                info.unit_price, info.tax_included = excl[0]["value"], False
                info.method = f"{info.method} → 「{excl[0]['label']}」を採用"
            else:
                info.tax_included = True
    # 複数サイズをまとめた商品グループのページ
    info.is_group = any("ProductGroup" in _types(d) for d in _iter_jsonld(html)) and \
        not any("Product" in _types(d) for d in _iter_jsonld(html))
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
    err, best = None, None
    try:
        html = fetch_static(url)
        best = parse(html, url, hint)
        best.html = html
        if best.unit_price is not None:
            return best
    except FetchError as e:
        err = e
    if not allow_render:
        if best is None:
            raise err
        return best
    for headless in (True, False):
        try:
            html = fetch_rendered(url, headless=headless)
        except FetchError as e:
            err = err or e
            continue
        r = parse(html, url, hint)
        r.rendered, r.html = True, html
        if best is None or r.unit_price is not None or len(r.candidates) > len(best.candidates):
            best = r
        if r.unit_price is not None or r.candidates:
            break
    if best is None:
        raise FetchError(f"{err}。ブラウザでの表示も失敗しました")
    return best


def net_price(info):
    """税込と分かっている場合は税抜に換算した単価。"""
    if info.unit_price is None:
        return None
    return round(info.unit_price / 1.1, 1) if info.tax_included else info.unit_price


def domain(url):
    return urllib.parse.urlparse(url).netloc.lower()
