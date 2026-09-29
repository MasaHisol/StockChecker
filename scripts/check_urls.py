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
