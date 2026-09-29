"""価格・納期情報の取得元 (プロバイダ)。

実運用では商社・通販サイトの API や社内の購買システムに合わせて
PriceProvider を継承したクラスを追加する。
"""
import csv
import hashlib
import io
import json
import urllib.parse
import urllib.request
from datetime import date


class PriceProvider:
    name = "base"

    def fetch(self, material):
        """観測値 dict のリストを返す。キー: unit_price, lead_time_days,
        stock_qty, min_order_qty, currency (いずれも任意)。"""
        raise NotImplementedError


class HttpJsonProvider(PriceProvider):
    """GET {url}?part_number=...&maker=... で JSON を返す価格フィード。

    応答は dict 1 件、または dict のリスト。
    """
    name = "http"

    def __init__(self, url, timeout=10):
        self.url = url
        self.timeout = timeout

    def fetch(self, material):
        q = urllib.parse.urlencode({"part_number": material["part_number"],
                                    "maker": material["maker"] or ""})
        sep = "&" if "?" in self.url else "?"
        with urllib.request.urlopen(f"{self.url}{sep}{q}", timeout=self.timeout) as r:
            data = json.load(r)
        return data if isinstance(data, list) else [data]


class DemoProvider(PriceProvider):
    """デモ用。品番と日付から決定的な擬似相場を生成する。"""
    name = "demo"

    def __init__(self, today=None):
        self.today = today or date.today()

    def fetch(self, material):
        seed = hashlib.sha256(material["part_number"].encode()).digest()
        base = 100 + int.from_bytes(seed[:2], "big") % 20000
        week = self.today.isocalendar()[1]
        drift = ((int.from_bytes(seed[2:3], "big") + week * 7) % 31 - 15) / 100
        return [{
            "unit_price": round(base * (1 + drift)),
            "lead_time_days": 3 + seed[3] % 60,
            "stock_qty": seed[4] * 4,
        }]


CSV_COLUMNS = ("part_number", "unit_price", "lead_time_days", "stock_qty",
               "min_order_qty", "currency", "observed_at")


def parse_price_csv(text):
    """価格表 CSV を読み込む。1 行目はヘッダ (part_number は必須)。"""
    rows = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        pn = (r.get("part_number") or "").strip()
        if not pn:
            continue
        rows.append({
            "part_number": pn,
            "unit_price": _num(r.get("unit_price"), float),
            "lead_time_days": _num(r.get("lead_time_days"), int),
            "stock_qty": _num(r.get("stock_qty"), int),
            "min_order_qty": _num(r.get("min_order_qty"), int),
            "currency": (r.get("currency") or "JPY").strip(),
            "observed_at": (r.get("observed_at") or "").strip() or None,
        })
    return rows


def _num(v, typ):
    v = (v or "").replace(",", "").strip()
    return typ(float(v)) if v else None
