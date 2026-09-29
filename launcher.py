"""exe 用ランチャー。ダブルクリックでサーバを起動し、ブラウザを開く。

- データ (DB・送信メール・設定) は exe と同じフォルダに保存する
- 設定は同じフォルダの stockchecker.ini (初回起動時に雛形を生成)
- 起動中は設定した間隔で定期チェックを自動実行する
"""
import configparser
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
INI = BASE / "stockchecker.ini"

TEMPLATE = """\
; StockChecker 設定ファイル。変更後はアプリを再起動してください。
[app]
port = 5000
open_browser = 1
; 定期チェックの間隔 (時間)。0 で無効
check_interval_hours = 24
; 価格取得にデモ用の擬似相場を使う (1/0)。[sources] 設定時は 0 のままにしてください
demo_prices = 0

[company]
company_name = 株式会社サンプル
purchaser_name = 購買担当
purchaser_email = purchasing@example.com

[smtp]
; 空欄の場合メールは送信せず outbox フォルダに .eml で保存します
host =
port = 587
user =
password =
starttls = 1

[sources]
; ネット上の一般価格・納期の自動取得。キーを入れた取得元だけが使われます
; ---- 無料 ----
; 部材に登録した商品ページ URL を巡回して価格を読む (キー不要)
page_watch = 1
; Yahoo!ショッピング: https://e.developer.yahoo.co.jp/ でアプリ登録 → Client ID
yahoo_app_id =
; 楽天市場: https://webservice.rakuten.co.jp/ でアプリ登録 → アプリ ID
rakuten_app_id =
; Mouser (電子部品): https://www.mouser.jp/api-hub/ で無料発行
mouser_api_key =
; Digi-Key (電子部品): https://developer.digikey.com/ でアプリ登録 (Production)
digikey_client_id =
digikey_client_secret =
; ---- 有料 (任意) ----
; Web 検索 (機構部品・鋼材・汎用品など何でも): https://console.anthropic.com/ で発行 (従量課金)
anthropic_api_key =
; off / fallback (API で見つからない部材のみ・推奨) / always
web_search = off
; 同じ部材を Web 検索する最短間隔 (日)。費用を抑えるため
web_search_interval_days = 7

[automation]
; 1 にすると「自動送信 ON」の仕入先へ見積依頼を承認なしで送信します
auto_send = 0
price_feed_url =

[rules]
stale_days = 30
price_change_pct = 10
high_value_amount = 100000
lead_margin_days = 3
"""

ENV_MAP = {
    ("company", "company_name"): "SC_COMPANY_NAME",
    ("company", "purchaser_name"): "SC_PURCHASER_NAME",
    ("company", "purchaser_email"): "SC_PURCHASER_EMAIL",
    ("smtp", "host"): "SC_SMTP_HOST",
    ("smtp", "port"): "SC_SMTP_PORT",
    ("smtp", "user"): "SC_SMTP_USER",
    ("smtp", "password"): "SC_SMTP_PASSWORD",
    ("smtp", "starttls"): "SC_SMTP_STARTTLS",
    ("sources", "page_watch"): "SC_PAGE_WATCH",
    ("sources", "yahoo_app_id"): "SC_YAHOO_APP_ID",
    ("sources", "rakuten_app_id"): "SC_RAKUTEN_APP_ID",
    ("sources", "mouser_api_key"): "SC_MOUSER_API_KEY",
    ("sources", "digikey_client_id"): "SC_DIGIKEY_CLIENT_ID",
    ("sources", "digikey_client_secret"): "SC_DIGIKEY_CLIENT_SECRET",
    ("sources", "anthropic_api_key"): "ANTHROPIC_API_KEY",
    ("sources", "web_search"): "SC_WEB_SEARCH",
    ("sources", "web_search_interval_days"): "SC_WEB_SEARCH_INTERVAL_DAYS",
    ("automation", "auto_send"): "SC_AUTO_SEND",
    ("automation", "price_feed_url"): "SC_PRICE_FEED_URL",
    ("rules", "stale_days"): "SC_STALE_DAYS",
    ("rules", "price_change_pct"): "SC_PRICE_CHANGE_PCT",
    ("rules", "high_value_amount"): "SC_HIGH_VALUE_AMOUNT",
    ("rules", "lead_margin_days"): "SC_LEAD_MARGIN_DAYS",
}


def load_config():
    if not INI.exists():
        INI.write_text(TEMPLATE, encoding="utf-8")
    user = configparser.ConfigParser()
    user.read(INI, encoding="utf-8")
    if not user.has_section("sources"):  # 旧バージョンの ini に新しい設定欄を追記
        block = TEMPLATE[TEMPLATE.index("\n[sources]") + 1:TEMPLATE.index("\n[automation]") + 1]
        with INI.open("a", encoding="utf-8") as f:
            f.write("\n" + block)
    cp = configparser.ConfigParser()
    cp.read_string(TEMPLATE)
    cp.read(INI, encoding="utf-8")
    for (sec, key), env in ENV_MAP.items():
        v = cp.get(sec, key, fallback="").strip()
        if v:
            os.environ.setdefault(env, v)
    os.environ.setdefault("SC_DATABASE", str(BASE / "stockchecker.db"))
    os.environ.setdefault("SC_OUTBOX_DIR", str(BASE / "outbox"))
    return cp


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def scheduler(settings, hours, demo):
    from stockchecker import db, service
    from stockchecker.app import build_providers
    while True:
        try:
            conn = db.connect(settings.database)
            s = service.run_checks(conn, settings, build_providers(settings, demo))
            conn.close()
            logging.info("定期チェック完了: %s", s)
        except Exception:
            logging.exception("定期チェックに失敗しました")
        time.sleep(hours * 3600)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cp = load_config()
    from stockchecker import create_app
    from stockchecker.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    port = free_port(cp.getint("app", "port"))
    url = f"http://127.0.0.1:{port}/"

    hours = cp.getfloat("app", "check_interval_hours")
    if hours > 0:
        threading.Thread(target=scheduler, daemon=True,
                         args=(settings, hours, cp.getboolean("app", "demo_prices"))).start()
    if cp.getboolean("app", "open_browser"):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    print("=" * 60)
    print(" 部材価格・納期トラッカー 起動中")
    print(f" ブラウザで {url} を開いてください")
    print(f" データ・設定フォルダ: {BASE}")
    print(" 終了するにはこのウィンドウを閉じてください")
    print("=" * 60)
    from waitress import serve
    serve(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
