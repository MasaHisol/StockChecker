"""CLI

  python -m stockchecker serve [--host 0.0.0.0 --port 5000]
  python -m stockchecker check [--demo] [--no-notify]   # 全部材の価格取得と判定 (任意で cron 等から)
  python -m stockchecker remind                         # 確認期限・回答待ちのリマインドを 1 回実行
  python -m stockchecker seed                           # デモデータ投入
"""
import argparse
import json
import logging

from . import db, service
from .app import build_providers, create_app
from .config import Settings


def seed(conn):
    c = conn
    if c.execute("SELECT COUNT(*) FROM materials").fetchone()[0]:
        print("既にデータがあります。")
        return
    c.execute("INSERT INTO staff (name, email) VALUES ('山田 太郎', 'yamada@example.com')")
    c.execute("INSERT INTO staff (name, email) VALUES ('佐藤 花子', 'sato@example.com')")
    c.execute("INSERT INTO suppliers (name, contact_name, email, auto_send_rfq) VALUES "
              "('株式会社部品商事', '鈴木', 'sales@buhin.example.com', 0)")
    c.execute("INSERT INTO suppliers (name, contact_name, email, auto_send_rfq) VALUES "
              "('電子パーツ販売株式会社', '田中', 'quote@parts.example.com', 1)")
    items = [
        ("STM32F407VGT6", "マイコン STM32F407", "STMicroelectronics", "LQFP100", 200, "2026-11-30", 1500, 0, 1, 2),
        ("GRM188R71H104KA93D", "積層セラミックコンデンサ 0.1uF", "村田製作所", "0603 50V X7R", 5000, "2026-10-31", 2, 0, 1, 2),
        ("SUS304-PL-3T", "ステンレス板 SUS304 t3", None, "1000x2000", 20, "2026-10-20", 30000, 0, 2, 1),
        ("BRKT-A-001", "取付ブラケット (特注)", None, "図面 A-001 Rev.B", 100, "2026-12-15", None, 1, 2, 1),
    ]
    for pn, name, maker, spec, qty, req, budget, custom, owner, sup in items:
        c.execute("INSERT INTO materials (part_number, name, maker, spec, quantity, required_date, "
                  "budget_unit_price, custom_item, owner_id, preferred_supplier_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (pn, name, maker, spec, qty, req, budget, custom, owner, sup))
    c.commit()
    import random
    from datetime import datetime, timedelta
    rnd = random.Random(1)
    base = {1: 1450, 2: 1.8, 3: 26000}
    for mid, p in base.items():  # 半年分の推移 (グラフ確認用)
        for w in range(26, -1, -2):
            p = round(p * (1 + rnd.uniform(-0.03, 0.045)), 2 if p < 100 else 0)
            at = (datetime.now() - timedelta(weeks=w)).strftime("%Y-%m-%d 09:00:00")
            db.add_observation(c, mid, "page" if mid != 3 else "quote", p, [60, 14, 7][mid - 1] + rnd.randint(-3, 5),
                               vendor=["Mouser", "モノタロウ", "株式会社部品商事"][mid - 1], observed_at=at)
    c.execute("UPDATE materials SET watch_urls='https://www.monotaro.com/p/0202/9991/' WHERE id=2")
    c.execute("UPDATE materials SET created_at='2026-05-01 09:00:00' WHERE id=4")
    c.commit()
    print("デモデータを投入しました。")


def main():
    ap = argparse.ArgumentParser(prog="stockchecker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=5000)
    cp = sub.add_parser("check")
    cp.add_argument("--demo", action="store_true", help="デモ用擬似相場で価格取得する")
    cp.add_argument("--no-notify", action="store_true")
    sub.add_parser("seed")
    sub.add_parser("remind")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = Settings.from_env()
    if args.cmd == "serve":
        settings.background = True
        create_app(settings).run(host=args.host, port=args.port, threaded=True)
        return
    conn = db.connect(settings.database)
    db.init_db(conn)
    if args.cmd == "seed":
        seed(conn)
    elif args.cmd == "remind":
        from . import reminders
        print(json.dumps(reminders.scan(conn, settings), ensure_ascii=False))
    elif args.cmd == "check":
        s = service.run_checks(conn, settings, build_providers(settings, args.demo),
                               notify=not args.no_notify)
        print(json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    main()
