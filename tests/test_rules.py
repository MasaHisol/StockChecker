from datetime import date

from stockchecker.config import Rules
from stockchecker.rules import evaluate, needs_quote

TODAY = date(2026, 9, 29)


def mat(**kw):
    m = dict(quantity=10, custom_item=0, budget_unit_price=None, required_date=None)
    m.update(kw)
    return m


def obs(price=100, lt=5, at="2026-09-28 10:00:00", stock=None, id=2):
    return dict(id=id, unit_price=price, lead_time_days=lt, observed_at=at, stock_qty=stock)


def codes(fs):
    return {f.code for f in fs}


def test_no_price_needs_quote():
    fs = evaluate(mat(), None, None, Rules(), TODAY)
    assert codes(fs) == {"no_price"} and needs_quote(fs)


def test_fresh_price_is_ok():
    assert evaluate(mat(), obs(), None, Rules(), TODAY) == []


def test_stale_price():
    fs = evaluate(mat(), obs(at="2026-07-01 00:00:00"), None, Rules(stale_days=30), TODAY)
    assert "stale_price" in codes(fs)


def test_price_change_and_budget_and_high_value():
    fs = evaluate(mat(quantity=1000, budget_unit_price=110), obs(price=120),
                  obs(price=100, id=1), Rules(), TODAY)
    assert {"price_change", "over_budget", "high_value"} <= codes(fs)


def test_lead_time():
    fs = evaluate(mat(required_date="2026-10-02"), obs(lt=10), None, Rules(), TODAY)
    assert "lead_time_over" in codes(fs)
    fs = evaluate(mat(required_date="2026-10-10"), obs(lt=10), None, Rules(lead_time_margin_days=3), TODAY)
    assert codes(fs) == {"lead_time_tight"} and not needs_quote(fs)


def test_custom_and_stock():
    fs = evaluate(mat(custom_item=1), obs(stock=3), None, Rules(), TODAY)
    assert {"custom_item", "stock_short"} <= codes(fs)
