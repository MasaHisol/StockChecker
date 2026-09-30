"""サイトごとのアクセス間隔の制御。

- サイト (ドメイン) ごとに最低待ち時間を空けてからアクセスする
- 制限 (HTTP 429 やエラーページ) を受けたサイトは、その回の取得中は待ち時間を倍に伸ばす
- 成功が続けば少しずつ元の待ち時間に戻す
"""
import random
import threading
import time

DEFAULT_DOMAIN_DELAYS = "misumi-ec.com=10\nmonotaro.com=3"


def parse_domain_delays(text):
    """「ドメイン=秒」を 1 行ずつ書いた設定を dict にする。"""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            try:
                out[k.strip().lower()] = float(v)
            except ValueError:
                pass
    return out


class DomainThrottle:
    MAX_PENALTY = 8.0

    def __init__(self, default_delay=3.0, overrides=None, jitter=0.25,
                 sleep=time.sleep, clock=time.monotonic):
        self.default_delay = float(default_delay)
        self.overrides = overrides or {}
        self.jitter = jitter
        self._sleep, self._clock = sleep, clock
        self._last = {}
        self._penalty = {}
        self._lock = threading.Lock()

    def base_delay(self, domain):
        domain = (domain or "").lower()
        best, best_len = self.default_delay, -1
        for key, sec in self.overrides.items():  # 一番詳しく一致した設定を使う
            if (domain == key or domain.endswith("." + key)) and len(key) > best_len:
                best, best_len = sec, len(key)
        return best

    def delay(self, domain):
        return self.base_delay(domain) * self._penalty.get(domain, 1.0)

    def wait(self, domain, should_stop=None):
        """前回アクセスから必要な時間が経つまで待つ。should_stop() が真なら途中で戻る。"""
        with self._lock:
            last = self._last.get(domain)
            d = self.delay(domain) * (1 + random.uniform(0, self.jitter))
        if last is not None:
            remain = last + d - self._clock()
            while remain > 0:
                if should_stop and should_stop():
                    return False
                self._sleep(min(remain, 1.0))
                remain = last + d - self._clock()
        with self._lock:
            self._last[domain] = self._clock()
        return True

    def penalize(self, domain):
        with self._lock:
            self._penalty[domain] = min(self._penalty.get(domain, 1.0) * 2, self.MAX_PENALTY)

    def reward(self, domain):
        with self._lock:
            p = self._penalty.get(domain, 1.0)
            self._penalty[domain] = max(1.0, p * 0.75)
