from stockchecker import pagereader as r


def test_jsonld_and_title():
    h = ('<html><head><title>T</title><script type="application/ld+json">{"@type":"Product",'
         '"name":"ボルト","mpn":"CB5-20","offers":{"@type":"Offer","price":"12.5"}}</script></head></html>')
    i = r.parse(h)
    assert i.unit_price == 12.5 and i.part_number == "CB5-20" and i.title == "ボルト"


def test_heuristic_skips_shipping_and_detects_tax():
    h = "<body><div>送料 ¥500</div><dl><dt>通常単価（税込）</dt><dd>1,650円</dd></dl></body>"
    i = r.parse(h)
    assert i.unit_price == 1650 and i.tax_included and r.net_price(i) == 1500


def test_hint_label_reused_and_title_excluded():
    h = ("<html><head><title>シャフト SFJ10-150</title></head><body><div>送料 ¥600</div>"
         "<div>標準単価(税別)：</div><div>¥2,640</div></body></html>")
    labels = [c["label"] for c in r.parse(h).candidates]
    assert "SFJ10-150" not in " ".join(labels)
    i = r.parse(h, hint={"label": "標準単価(税別)：", "tax": "excl"})
    assert i.unit_price == 2640 and i.method.startswith("記憶")


def test_lead_time_and_stock():
    i = r.parse("<div>在庫あり</div><div>通常 3営業日 出荷</div>")
    assert i.in_stock is True and i.lead_time_days == 4


def test_bad_url():
    import pytest
    with pytest.raises(r.FetchError):
        r.read("ftp://x")
