from stockchecker.free_sources import (RakutenProvider, YahooShoppingProvider,
                                       name_contains_pn, parse_product_page)

MAT = {"part_number": "SFJ10-100", "quantity": 5, "watch_urls": None}


def test_jsonld_offer():
    html = '''<html><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"シャフト",
     "offers":{"@type":"Offer","price":"1,234","priceCurrency":"JPY",
       "availability":"https://schema.org/InStock"}}</script>
    <p>通常 3営業日 出荷</p></html>'''
    r = parse_product_page(html)
    assert r["unit_price"] == 1234 and r["in_stock"] is True and r["lead_time_days"] == 4


def test_meta_price_fallback():
    r = parse_product_page('<meta property="product:price:amount" content="980">')
    assert r["unit_price"] == 980


def test_name_match():
    assert name_contains_pn("ミスミ シャフト SFJ 10-100 1本", "SFJ10-100")
    assert not name_contains_pn("SFJ10-150", "SFJ10-100")


def test_yahoo_parse():
    res = {"hits": [{"name": "SFJ10-100 シャフト", "price": 1100, "url": "u", "seller": {"name": "店A"}},
                    {"name": "別商品", "price": 10}]}
    o = YahooShoppingProvider.parse(res, MAT)
    assert len(o) == 1 and o[0]["unit_price"] == 1000 and o[0]["vendor"] == "Yahoo!/店A"


def test_rakuten_parse():
    res = {"Items": [{"Item": {"itemName": "SFJ10-100", "itemPrice": 2200, "taxFlag": 0,
                               "itemUrl": "u", "shopName": "店B"}}]}
    assert RakutenProvider.parse(res, MAT)[0]["unit_price"] == 2000
