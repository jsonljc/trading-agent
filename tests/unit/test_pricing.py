from skills.execution._pricing import marketable_limit, marketable_sell_limit


def test_marketable_limit_is_ask_plus_cap_rounded_up_to_penny():
    # 100.00 ask, 1% cap -> 101.00
    assert marketable_limit(100.0, 0.01) == 101.0
    # 5.10 ask, 5% cap -> 5.355 -> ceil to 5.36
    assert marketable_limit(5.10, 0.05) == 5.36
    # zero cap returns the ask unchanged (already a penny)
    assert marketable_limit(2.50, 0.0) == 2.50


def test_marketable_limit_never_below_ask():
    # rounding is ceil, so the limit is always >= ask (stays marketable)
    for ask in (0.07, 1.23, 14.99, 250.01):
        assert marketable_limit(ask, 0.01) >= ask


def test_marketable_sell_limit_is_price_minus_cap_rounded_down():
    # 105.00 price, 1% cap -> 103.95
    assert marketable_sell_limit(105.0, 0.01) == 103.95
    # rounds DOWN so the limit is always <= price (stays marketable)
    for price in (0.07, 1.23, 14.99, 250.01):
        assert marketable_sell_limit(price, 0.01) <= price
