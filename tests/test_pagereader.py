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


def test_table_column_price_and_misumi_url():
    h = ("<table><tr><th>型番</th><th>通常単価(税別)</th><th>出荷日</th></tr>"
         "<tr><td>CP30-BA 2P 1-M 10A</td><td>¥ 6,480</td><td>通常出荷日 4日目</td></tr></table>")
    i = r.parse(h, "https://jp.misumi-ec.com/vona2/detail/1/?HissuCode=CP30-BA+2P+1-M+10A")
    assert i.unit_price == 6480 and i.lead_time_days == 4
    assert i.part_number == "CP30-BA 2P 1-M 10A" and not i.tax_included


MONOTARO_P = (  # モノタロウ商品ページ (/p/) の実際の構造を簡略化したもの
    '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
    '"name":"フラットワッシャー 12M-FW","sku":"12M-FW","offers":{"@type":"Offer","price":241,'
    '"availability":"https://schema.org/InStock"}}</script>'
    '<div class="PriceArea"><span><span>参考基準価格(税別)</span>￥230</span>'
    '<span><span>販売価格(税込)</span>￥241</span>'
    '<div><span>販売価格(税別)</span></div><div><span><span>￥</span>219</span></div></div>'
    '<div>3,500円(税別)以上で配送料無料</div>')


def test_monotaro_product_page_uses_tax_excluded_price():
    i = r.parse(MONOTARO_P)
    assert i.unit_price == 219 and i.tax_included is False and r.net_price(i) == 219
    assert i.part_number == "12M-FW" and i.in_stock


def test_product_group_page_flagged():
    h = '<script type="application/ld+json">{"@type":"ProductGroup","name":"フラットワッシャー"}</script><div>￥220</div>'
    assert r.parse(h).is_group
