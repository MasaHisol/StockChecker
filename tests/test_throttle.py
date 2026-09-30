from stockchecker.throttle import DomainThrottle, parse_domain_delays


def test_domain_delay_overrides_and_penalty():
    t = DomainThrottle(3, parse_domain_delays("misumi-ec.com=10\njp.misumi-ec.com=12\nbad"))
    assert t.base_delay("jp.misumi-ec.com") == 12
    assert t.base_delay("www.misumi-ec.com") == 10
    assert t.base_delay("www.monotaro.com") == 3
    t.penalize("x.com")
    t.penalize("x.com")
    assert t.delay("x.com") == 12
    t.reward("x.com")
    assert t.delay("x.com") == 9


def test_wait_spaces_requests():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    t = DomainThrottle(5, jitter=0, sleep=sleep, clock=lambda: now[0])
    t.wait("a.com")
    t.wait("a.com")
    t.wait("b.com")  # 別サイトは待たない
    assert abs(sum(slept) - 5) < 1e-9
