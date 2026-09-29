import os
from dataclasses import dataclass, field


def _env_bool(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@dataclass
class Rules:
    """見積必須と判定するしきい値。環境変数で上書き可能。"""
    stale_days: int = 30             # 価格情報がこの日数より古ければ見積必須
    price_change_pct: float = 10.0   # 前回比の変動率(%)がこれ以上なら見積必須
    high_value_amount: float = 100000.0  # 概算金額(単価×数量)がこれ以上なら見積必須
    lead_time_margin_days: int = 3   # 必要納期までの余裕がこれ未満なら要注意


@dataclass
class Settings:
    database: str = "stockchecker.db"
    company_name: str = "株式会社サンプル"
    purchaser_name: str = "購買担当"
    purchaser_email: str = "purchasing@example.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    # SMTP 未設定時や dry-run 時はこのフォルダに .eml を書き出す
    outbox_dir: str = "outbox"
    # 見積依頼メールの自動送信を全体で許可するか (仕入先ごとの設定と AND)
    auto_send_enabled: bool = False
    price_feed_url: str = ""   # HTTP 価格フィードの URL (任意)
    # ネット自動取得
    mouser_api_key: str = ""
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    yahoo_app_id: str = ""
    rakuten_app_id: str = ""
    page_watch: bool = True     # 商品ページ URL の巡回 (キー不要)
    anthropic_api_key: str = ""
    # Web 検索 (Claude): off / fallback (API で見つからない部材のみ) / always
    web_search: str = "fallback"
    web_search_interval_days: int = 7   # 同じ部材を Web 検索する最短間隔 (費用抑制)
    rules: Rules = field(default_factory=Rules)

    @classmethod
    def from_env(cls):
        s = cls()
        e = os.environ.get
        s.database = e("SC_DATABASE", s.database)
        s.company_name = e("SC_COMPANY_NAME", s.company_name)
        s.purchaser_name = e("SC_PURCHASER_NAME", s.purchaser_name)
        s.purchaser_email = e("SC_PURCHASER_EMAIL", s.purchaser_email)
        s.smtp_host = e("SC_SMTP_HOST", s.smtp_host)
        s.smtp_port = int(e("SC_SMTP_PORT", s.smtp_port))
        s.smtp_user = e("SC_SMTP_USER", s.smtp_user)
        s.smtp_password = e("SC_SMTP_PASSWORD", s.smtp_password)
        s.smtp_starttls = _env_bool("SC_SMTP_STARTTLS", s.smtp_starttls)
        s.outbox_dir = e("SC_OUTBOX_DIR", s.outbox_dir)
        s.auto_send_enabled = _env_bool("SC_AUTO_SEND", s.auto_send_enabled)
        s.price_feed_url = e("SC_PRICE_FEED_URL", s.price_feed_url)
        s.mouser_api_key = e("SC_MOUSER_API_KEY", s.mouser_api_key)
        s.digikey_client_id = e("SC_DIGIKEY_CLIENT_ID", s.digikey_client_id)
        s.digikey_client_secret = e("SC_DIGIKEY_CLIENT_SECRET", s.digikey_client_secret)
        s.yahoo_app_id = e("SC_YAHOO_APP_ID", s.yahoo_app_id)
        s.rakuten_app_id = e("SC_RAKUTEN_APP_ID", s.rakuten_app_id)
        s.page_watch = _env_bool("SC_PAGE_WATCH", s.page_watch)
        s.anthropic_api_key = e("ANTHROPIC_API_KEY", s.anthropic_api_key)
        s.web_search = e("SC_WEB_SEARCH", s.web_search).lower()
        s.web_search_interval_days = int(e("SC_WEB_SEARCH_INTERVAL_DAYS", s.web_search_interval_days))
        r = s.rules
        r.stale_days = int(e("SC_STALE_DAYS", r.stale_days))
        r.price_change_pct = float(e("SC_PRICE_CHANGE_PCT", r.price_change_pct))
        r.high_value_amount = float(e("SC_HIGH_VALUE_AMOUNT", r.high_value_amount))
        r.lead_time_margin_days = int(e("SC_LEAD_MARGIN_DAYS", r.lead_time_margin_days))
        return s
