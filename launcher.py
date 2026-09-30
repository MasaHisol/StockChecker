"""exe 用ランチャー。ダブルクリックでサーバを起動し、ブラウザを開く。

- データ (DB・送信メール・設定) は exe と同じフォルダに保存する
- 設定は同じフォルダの stockchecker.ini (初回起動時に雛形を生成)
- 価格・納期の取得は画面の「一括取得」ボタンで行う (自動では取得しない)
- 起動中は 30 分ごとに「確認期限」「回答待ち」のリマインドだけを確認する
- share = 1 にすると社内ネットワークの他の PC からも使える (チーム共有)
"""
import configparser
import logging
import os
import re
import socket
import sys
import threading
import webbrowser
from pathlib import Path

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
INI = BASE / "stockchecker.ini"

TEMPLATE = """\
; StockChecker 設定ファイル。変更後はアプリを再起動してください。
[app]
port = 5000
open_browser = 1
; 1 にすると同じ社内ネットワークの PC からブラウザで使えます (チーム共有)。
; 常時起動している PC で有効にし、起動時に表示されるアドレスをメンバーに伝えてください
share = 0
; リマインド (確認期限・回答待ち) を確認する間隔 (分)
reminder_interval_minutes = 30

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

[imap]
; 仕入先からの返信を「受信メール」画面のボタンで取り込む (任意)。空欄なら .eml/.msg のアップロードのみ
; 例: Gmail は imap.gmail.com / Microsoft 365 は outlook.office365.com (アプリ パスワードが必要な場合あり)
host =
port = 993
user =
password =
folder = INBOX
ssl = 1

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
    ("imap", "host"): "SC_IMAP_HOST",
    ("imap", "port"): "SC_IMAP_PORT",
    ("imap", "user"): "SC_IMAP_USER",
    ("imap", "password"): "SC_IMAP_PASSWORD",
    ("imap", "folder"): "SC_IMAP_FOLDER",
    ("imap", "ssl"): "SC_IMAP_SSL",
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
    # 旧バージョンの ini に無い設定欄 ([sources] [imap] など) を末尾に追記する
    sections = re.findall(r"^\[(\w+)\]", TEMPLATE, re.M)
    for i, sec in enumerate(sections):
        if not user.has_section(sec):
            start = TEMPLATE.index(f"\n[{sec}]") + 1
            end = TEMPLATE.index(f"\n[{sections[i + 1]}]") + 1 if i + 1 < len(sections) else len(TEMPLATE)
            with INI.open("a", encoding="utf-8") as f:
                f.write("\n" + TEMPLATE[start:end])
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


def lan_addresses():
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 実際には送信しない (経路からアドレスを得る)
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    for stream in (sys.stdout, sys.stderr):  # 文字コードの合わないコンソールでも落ちないように
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cp = load_config()
    from stockchecker import create_app
    from stockchecker.config import Settings

    settings = Settings.from_env()
    settings.background = True
    settings.reminder_interval_minutes = max(1, cp.getint("app", "reminder_interval_minutes", fallback=30))
    share = cp.getboolean("app", "share", fallback=False)
    port = free_port(cp.getint("app", "port"))
    url = f"http://127.0.0.1:{port}/"
    if share:
        settings.lan_urls = [f"http://{socket.gethostname()}:{port}/"] + \
            [f"http://{ip}:{port}/" for ip in lan_addresses()]
    app = create_app(settings)
    if cp.getboolean("app", "open_browser"):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    print("=" * 60)
    print(" 部材価格・納期トラッカー 起動中")
    print(f" ブラウザで {url} を開いてください")
    if share:
        print(" チーム共有: 同じ社内ネットワークの PC からは次のアドレスで使えます")
        for u in settings.lan_urls:
            print(f"   {u}")
    print(f" データ・設定フォルダ: {BASE}")
    print(" 終了するにはこのウィンドウを閉じてください")
    print("=" * 60)
    from waitress import serve
    serve(app, host="0.0.0.0" if share else "127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
