"""ボタン操作で実行する「価格・納期の一括取得」。

登録部材の商品ページ URL (と設定済みの API) から価格・納期を順番に取得する。
- 1 回に実行できる一括取得は 1 つだけ (同時に押されたら実行中のものを返す)
- サイトごとに待ち時間を空け、制限を受けたら待ち時間を伸ばして再試行する
- 途中で中止できる。進み具合は fetch_jobs / fetch_job_items に記録する
"""
import json
import logging
import threading
import time

from . import db, free_sources, online, pagereader, service, sources, throttle

log = logging.getLogger(__name__)

_lock = threading.Lock()

FETCH_DEFAULTS = {
    "fetch_default_delay": "3",        # 同じサイトへのアクセス間隔 (秒)
    "fetch_domain_delays": throttle.DEFAULT_DOMAIN_DELAYS,
    "fetch_max_retries": "3",          # 制限を受けたときの再試行回数
    "fetch_backoff_seconds": "30",     # 制限を受けたときの最初の待ち時間 (回ごとに倍)
}


def fetch_config(conn):
    return {k: db.get_setting(conn, k, v) for k, v in FETCH_DEFAULTS.items()}


def make_throttle(conn, sleep=time.sleep):
    cfg = fetch_config(conn)
    return throttle.DomainThrottle(float(cfg["fetch_default_delay"]),
                                   throttle.parse_domain_delays(cfg["fetch_domain_delays"]),
                                   sleep=sleep)


def current_job(conn):
    return conn.execute("SELECT * FROM fetch_jobs WHERE status IN ('running','cancelling') "
                        "AND started_at >= datetime('now','localtime','-6 hours') "
                        "ORDER BY id DESC LIMIT 1").fetchone()


def target_materials(conn, material_ids=None):
    q = "SELECT * FROM materials WHERE active=1"
    args = []
    if material_ids:
        q += f" AND id IN ({','.join('?' * len(material_ids))})"
        args = list(material_ids)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def start(settings, user_id=None, material_ids=None, run_async=True, sleep=time.sleep, actor=None):
    """一括取得を開始してジョブ ID を返す。実行中なら既存の ID を返す (新規は False)。"""
    with _lock:
        conn = db.connect(settings.database)
        try:
            cur = current_job(conn)
            if cur:
                return cur["id"], False
            mats = target_materials(conn, material_ids)
            job_id = conn.execute("INSERT INTO fetch_jobs (total, started_by, started_by_name) VALUES (?, ?, ?)",
                                  (len(mats), user_id, actor)).lastrowid
            conn.commit()
            db.log_activity(conn, user_id, "fetch_start", detail=f"{len(mats)} 件の一括取得を開始", actor=actor)
        finally:
            conn.close()
    args = (settings, job_id, [m["id"] for m in mats], sleep)
    if run_async:
        threading.Thread(target=run, args=args, daemon=True, name=f"fetch-job-{job_id}").start()
    else:
        run(*args)
    return job_id, True


def cancel(conn, job_id):
    conn.execute("UPDATE fetch_jobs SET status='cancelling' WHERE id=? AND status='running'", (job_id,))
    conn.commit()


class Fetcher:
    """1 部材ずつ価格を取得する (一括取得・部材画面の「最新価格を取得」で共用)。"""

    def __init__(self, conn, settings, throttle_=None, session=None, should_stop=None,
                 sleep=time.sleep):
        self.conn, self.settings = conn, settings
        self.throttle = throttle_ or make_throttle(conn, sleep)
        self.session = session
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep
        cfg = fetch_config(conn)
        self.max_retries = int(cfg["fetch_max_retries"])
        self.backoff = float(cfg["fetch_backoff_seconds"])
        self.api = sources.api_providers(settings)
        self.web = sources.web_search_provider(settings)
        self.hints = {r["domain"]: dict(r) for r in conn.execute("SELECT * FROM site_hints")}

    def _pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.should_stop():
                return False
            self.sleep(min(1.0, end - time.monotonic()))
        return True

    def read_url(self, url, quantity):
        dom = pagereader.domain(url)
        for attempt in range(self.max_retries + 1):
            if not self.throttle.wait(dom, self.should_stop):
                raise pagereader.FetchError("中止しました")
            try:
                info = pagereader.read(url, self.hints.get(dom), self.settings.page_watch_render,
                                       session=self.session)
            except pagereader.RateLimited:
                self.throttle.penalize(dom)
                if attempt >= self.max_retries:
                    raise pagereader.RateLimited(
                        f"{dom} からアクセス制限を受けました ({self.max_retries} 回再試行)。"
                        "時間を置くか、設定でこのサイトの待ち時間を長くしてください")
                wait = self.backoff * (2 ** attempt)
                log.info("rate limited by %s, waiting %.0fs", dom, wait)
                if not self._pause(wait):
                    raise pagereader.FetchError("中止しました")
                continue
            self.throttle.reward(dom)
            if info.is_group:
                raise pagereader.FetchError("サイズ違いをまとめた一覧ページです。型番ごとのページ URL を登録してください")
            if info.unit_price is None and info.lead_time_days is None:
                raise pagereader.FetchError("価格を読み取れませんでした (部材画面で価格の場所を指定してください)")
            return free_sources.info_to_offer(info, quantity)

    def fetch_material(self, m, job_id=None, user_id=None):
        """1 部材の価格を取得・記録する。戻り値: (status, best_offer, messages)"""
        qty = m["quantity"] or 1
        urls = [u.strip() for u in (m["watch_urls"] or "").splitlines() if u.strip()]
        offers, errors = [], []
        if self.settings.page_watch:
            for url in urls:
                try:
                    o = self.read_url(url, qty)
                    o["source"] = "page"
                    offers.append(o)
                except pagereader.FetchError as e:
                    errors.append(f"{pagereader.domain(url)}: {pagereader.short(e, 300)}")
        for p in self.api:
            if not self.throttle.wait(p.name, self.should_stop):
                break
            try:
                for o in p.fetch(m):
                    o.setdefault("source", p.name)
                    offers.append(o)
            except Exception as e:
                errors.append(f"{p.name}: {pagereader.short(e)}")
        if self.web and not any(o.get("unit_price") is not None for o in offers) and \
                (self.settings.web_search == "always" or not urls) and \
                not service._web_recently_checked(self.conn, m["id"], self.settings.web_search_interval_days):
            try:
                for o in self.web.fetch(m):
                    o.setdefault("source", "web")
                    offers.append(o)
            except Exception as e:
                errors.append(f"Web 検索: {e}")
        if not urls and not self.api and not offers and not errors:
            return "skipped", None, ["商品ページ URL が未登録です (メールでの確認対象)"]
        best = online.pick_best(offers, qty)
        if not best:
            return "error", None, errors or ["価格情報が見つかりませんでした"]
        db.add_observation(
            self.conn, m["id"], "+".join(sorted({o["source"] for o in offers})),
            unit_price=best.get("unit_price"), lead_time_days=best.get("lead_time_days"),
            stock_qty=best.get("stock_qty"), min_order_qty=best.get("min_order_qty"),
            currency=best.get("currency", "JPY"), vendor=best.get("vendor"), url=best.get("url"),
            detail=json.dumps(offers, ensure_ascii=False) if len(offers) > 1 or best.get("note") else None,
            job_id=job_id, created_by=user_id)
        return "ok", best, errors


def run(settings, job_id, material_ids, sleep=time.sleep):
    conn = db.connect(settings.database)
    stopped = lambda: conn.execute("SELECT status FROM fetch_jobs WHERE id=?",  # noqa: E731
                                   (job_id,)).fetchone()["status"] == "cancelling"
    session = pagereader.BrowserSession() if settings.page_watch_render else None
    try:
        fetcher = Fetcher(conn, settings, session=session, should_stop=stopped, sleep=sleep)
        jr = conn.execute("SELECT started_by, started_by_name FROM fetch_jobs WHERE id=?", (job_id,)).fetchone()
        user, who = jr["started_by"], jr["started_by_name"]
        for mid in material_ids:
            if stopped():
                break
            m = conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
            if m is None:
                continue
            conn.execute("UPDATE fetch_jobs SET current=? WHERE id=?",
                         (f"{m['part_number']} {m['name']}", job_id))
            conn.commit()
            try:
                status, best, msgs = fetcher.fetch_material(m, job_id, user)
            except Exception as e:  # 想定外のエラーでも次の部材へ進む
                log.exception("fetch failed for %s", m["part_number"])
                status, best, msgs = "error", None, [pagereader.short(e)]
            conn.execute(
                "INSERT INTO fetch_job_items (job_id, material_id, status, unit_price, lead_time_days, message) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, mid, status, best and best.get("unit_price"),
                 best and best.get("lead_time_days"), " / ".join(msgs) or None))
            col = {"ok": "ok", "error": "failed", "skipped": "skipped"}[status]
            conn.execute(f"UPDATE fetch_jobs SET done=done+1, {col}={col}+1 WHERE id=?", (job_id,))
            conn.commit()
        # 取得結果を判定ルールにかけ、アラート・担当者通知・見積依頼の下書きを作る
        summary = service.run_checks(conn, settings, providers=(), notify=True)
        cancelled = stopped()
        job = conn.execute("SELECT * FROM fetch_jobs WHERE id=?", (job_id,)).fetchone()
        msg = (f"取得 {job['ok']} 件 / 失敗 {job['failed']} 件 / 対象外 {job['skipped']} 件 ・ "
               f"新しいアラート {summary['new_alerts']} 件 ・ 見積依頼の下書き {summary['rfq_drafts']} 件")
        conn.execute("UPDATE fetch_jobs SET status=?, message=?, current=NULL, "
                     "finished_at=datetime('now','localtime') WHERE id=?",
                     ("cancelled" if cancelled else "done", msg, job_id))
        conn.commit()
        db.log_activity(conn, user, "fetch_done", detail=("中止: " if cancelled else "") + msg, actor=who)
    except Exception as e:
        log.exception("fetch job failed")
        conn.execute("UPDATE fetch_jobs SET status='failed', message=?, "
                     "finished_at=datetime('now','localtime') WHERE id=?", (str(e), job_id))
        conn.commit()
    finally:
        if session:
            session.close()
        conn.close()
