"""設定済みの価格取得元 (ページ監視以外の API 系) の組み立て。"""
from . import free_sources, online
from .providers import DemoProvider, HttpJsonProvider


def api_providers(settings, demo=False):
    """品番で検索する API 系の取得元。ページ監視 (URL) は jobs 側で個別に扱う。"""
    ps = []
    if settings.price_feed_url:
        ps.append(HttpJsonProvider(settings.price_feed_url))
    if settings.yahoo_app_id:
        ps.append(free_sources.YahooShoppingProvider(settings.yahoo_app_id))
    if settings.rakuten_app_id:
        ps.append(free_sources.RakutenProvider(settings.rakuten_app_id))
    if settings.mouser_api_key:
        ps.append(online.MouserProvider(settings.mouser_api_key))
    if settings.digikey_client_id and settings.digikey_client_secret:
        ps.append(online.DigiKeyProvider(settings.digikey_client_id, settings.digikey_client_secret))
    if demo:
        ps.append(DemoProvider())
    return ps


def web_search_provider(settings):
    if settings.anthropic_api_key and settings.web_search != "off":
        try:
            return online.WebSearchProvider(settings.anthropic_api_key)
        except ImportError:
            return None
    return None


def build_providers(settings, demo=False):
    """旧互換: ページ監視・API・Web 検索をまとめたリスト (CLI の check で使用)。"""
    ps = []
    if settings.page_watch:
        ps.append(free_sources.PageWatchProvider(settings.database))
    ps += api_providers(settings, demo)
    web = web_search_provider(settings)
    if web:
        web.fallback_only = True
        ps.append(web)
    return ps


def source_status(settings):
    return [("商品ページ監視", settings.page_watch),
            ("Yahoo!ショッピング", bool(settings.yahoo_app_id)),
            ("楽天市場", bool(settings.rakuten_app_id)),
            ("Mouser API", bool(settings.mouser_api_key)),
            ("Digi-Key API", bool(settings.digikey_client_id and settings.digikey_client_secret)),
            (f"Web 検索 (AI, {settings.web_search})",
             bool(settings.anthropic_api_key) and settings.web_search != "off"),
            ("価格フィード URL", bool(settings.price_feed_url))]
