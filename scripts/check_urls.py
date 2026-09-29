"""CI 用: 実サイトの商品ページが読み取れるか確認し、結果と HTML を保存する。"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from stockchecker import pagereader  # noqa: E402

out = pathlib.Path("url-check")
out.mkdir(exist_ok=True)
results = []
for i, url in enumerate(sys.argv[1:]):
    # 切り分け用: 静的取得と画面なし/画面ありブラウザそれぞれのページタイトル
    diag = {}
    for mode in ("static", "headless", "headed"):
        try:
            h = pagereader.fetch_static(url) if mode == "static" else \
                pagereader.fetch_rendered(url, headless=(mode == "headless"))
            m = pagereader.re.search(r"<title[^>]*>(.*?)</title>", h, pagereader.re.S)
            diag[mode] = {"title": m and m.group(1).strip()[:80], "bytes": len(h),
                          "yen": len(pagereader.PRICE_RE.findall(pagereader.visible_text(h)))}
            (out / f"page{i}-{mode}.html").write_text(h, encoding="utf-8")
        except Exception as e:
            diag[mode] = {"error": str(e)[:200]}
    print(json.dumps({"url": url, "diag": diag}, ensure_ascii=False, indent=1))
    try:
        info = pagereader.read(url)
        (out / f"page{i}.html").write_text(info.html or "", encoding="utf-8")
        res = {"url": url, "price": info.unit_price, "method": info.method,
               "tax_included": info.tax_included, "lead_time_days": info.lead_time_days,
               "in_stock": info.in_stock, "part_number": info.part_number, "title": info.title,
               "rendered": info.rendered, "candidates": info.candidates[:15]}
    except Exception as e:
        res = {"url": url, "error": str(e)}
    results.append(res)
    print(json.dumps(res, ensure_ascii=False, indent=1))
(out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
