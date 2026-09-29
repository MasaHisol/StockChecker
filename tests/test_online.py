import json
from types import SimpleNamespace

from stockchecker import db, service
from stockchecker.config import Settings
from stockchecker.online import (DigiKeyProvider, MouserProvider, WebSearchProvider,
                                 pick_best, price_for_qty)

MAT = {"id": 1, "part_number": "STM32F407VGT6", "name": "MCU", "maker": "ST", "spec": None,
       "quantity": 100, "unit": "個"}


def test_price_for_qty():
    assert price_for_qty([(1, 10), (10, 8), (100, 6)], 50) == 8
    assert price_for_qty([(10, 8)], 1) == 8


def test_mouser_parse():
    part = {"ManufacturerPartNumber": "STM32F407VGT6", "Availability": "1,234 In Stock",
            "AvailabilityInStock": "1234", "LeadTime": "84 Days", "Min": "1",
            "ProductDetailUrl": "https://mouser.jp/x",
            "PriceBreaks": [{"Quantity": 1, "Price": "¥2,100", "Currency": "JPY"},
                            {"Quantity": 100, "Price": "¥1,650", "Currency": "JPY"}]}
    o = MouserProvider.parse_part(part, MAT)
    assert o["unit_price"] == 1650 and o["stock_qty"] == 1234 and o["lead_time_days"] == 5
    part["AvailabilityInStock"] = "0"
    assert MouserProvider.parse_part(part, MAT)["lead_time_days"] == 84
    assert MouserProvider.parse_part({**part, "ManufacturerPartNumber": "OTHER"}, MAT) is None


def test_digikey_parse():
    p = {"ManufacturerProductNumber": "STM32F407VGT6", "QuantityAvailable": 10,
         "ManufacturerLeadWeeks": "12", "ProductUrl": "https://digikey.jp/x",
         "ProductVariations": [{"StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 2000},
                                                    {"BreakQuantity": 100, "UnitPrice": 1600}]}]}
    o = DigiKeyProvider.parse_product(p, MAT)
    assert o["unit_price"] == 1600 and o["lead_time_days"] == 84


class FakeClient:
    def __init__(self, text):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.text = text

    def create(self, **kw):
        self.calls += 1
        assert kw["tools"][0]["type"] == "web_search_20260209"
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=self.text)])


def test_web_search_provider():
    text = "調べました。\n" + json.dumps({"offers": [
        {"vendor": "モノタロウ", "url": "https://x", "unit_price": 1800, "lead_time_days": 3,
         "stock_qty": None, "note": ""},
        {"vendor": "不明", "unit_price": None, "lead_time_days": None}], "summary": "s"})
    offers = WebSearchProvider(client=FakeClient(text)).fetch(MAT)
    assert len(offers) == 1 and offers[0]["vendor"] == "モノタロウ"


def test_pick_best_prefers_stock():
    offers = [{"unit_price": 100, "stock_qty": 0, "lead_time_days": 60, "vendor": "A"},
              {"unit_price": 120, "stock_qty": 500, "lead_time_days": 5, "vendor": "B"}]
    assert pick_best(offers, 100)["vendor"] == "B"
    assert pick_best(offers, 1000)["vendor"] == "A"


class Static:
    def __init__(self, name, offers, fallback=False):
        self.name, self.offers, self.fallback_only, self.calls = name, offers, fallback, 0

    def fetch(self, m):
        self.calls += 1
        return [dict(o) for o in self.offers]


def test_refresh_uses_web_only_as_fallback(tmp_path):
    s = Settings(database=str(tmp_path / "t.db"))
    conn = db.connect(s.database)
    db.init_db(conn)
    conn.execute("INSERT INTO materials (part_number,name,quantity) VALUES ('A','a',1)")
    conn.execute("INSERT INTO materials (part_number,name,quantity) VALUES ('B','b',1)")
    conn.commit()
    mats = conn.execute("SELECT * FROM materials").fetchall()

    class Api(Static):
        def fetch(self, m):
            return [{"unit_price": 10, "vendor": "api"}] if m["part_number"] == "A" else []

    web = Static("web", [{"unit_price": 20, "vendor": "web", "url": "u"}], fallback=True)
    assert service.refresh_prices(conn, [Api("api", []), web], mats, s) == 2
    assert web.calls == 1  # B だけ Web 検索
    row = db.latest_observation(conn, 2)
    assert row["vendor"] == "web" and row["url"] == "u"
    service.refresh_prices(conn, [Api("api", []), web], mats, s)
    assert web.calls == 1  # 間隔内は再検索しない
