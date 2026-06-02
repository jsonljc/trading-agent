# tests/unit/test_pnl_attribution.py
from agent.pnl_attribution import compute_attribution, AttributionReport


def test_empty_input_returns_empty_report():
    report = compute_attribution([], [], [])
    assert isinstance(report, AttributionReport)
    assert report.sources == []
    assert report.grand_total == 0.0
    assert report.total_closed_lots == 0
    assert report.total_wins == 0
    assert report.win_rate == 0.0


def _entry(intent_id, channel="stp", ticker="NVDA", itype="equity",
           fill_price=100.0, fill_qty=10):
    return {"intent_id": intent_id, "channel": channel, "ticker": ticker,
            "instrument_type": itype, "fill_price": fill_price, "fill_qty": fill_qty}


def _sell(intent_id, sold_qty=10, sold_avg_price=110.0):
    return {"intent_id": intent_id, "sold_qty": sold_qty,
            "sold_avg_price": sold_avg_price}


def test_full_close_gain():
    # bought 10 @ 100, sold 10 @ 110 -> +100
    report = compute_attribution(
        [_entry("a")], [_sell("a", 10, 110.0)], [])
    assert len(report.sources) == 1
    s = report.sources[0]
    assert s.channel == "stp"
    assert s.realized == 100.0
    assert s.by_instrument.equity == 100.0
    assert report.grand_total == 100.0


def test_full_close_loss():
    # bought 10 @ 100, sold 10 @ 90 -> -100
    report = compute_attribution([_entry("a")], [], [_sell("a", 10, 90.0)])
    assert report.sources[0].realized == -100.0


def test_partial_close_uses_sold_qty_not_fill_qty():
    # bought 10 @ 100, sold only 4 @ 110 -> +40 (not +100)
    report = compute_attribution([_entry("a")], [_sell("a", 4, 110.0)], [])
    assert report.sources[0].realized == 40.0


def test_trim_and_followsell_mix_on_one_lot():
    # bought 10 @ 100; trim 3 @ 110 (+30); follow-sell 5 @ 120 (+100) -> +130
    report = compute_attribution(
        [_entry("a")], [_sell("a", 3, 110.0)], [_sell("a", 5, 120.0)])
    assert report.sources[0].realized == 130.0


def test_option_lot_applies_100x_multiplier():
    # synthetic: option bought 1 @ 2.00, sold 1 @ 3.00 -> (3-2)*1*100 = +100
    e = _entry("opt", itype="option", fill_price=2.0, fill_qty=1)
    report = compute_attribution([e], [_sell("opt", 1, 3.0)], [])
    s = report.sources[0]
    assert s.realized == 100.0
    assert s.by_instrument.option == 100.0
    assert s.by_instrument.equity == 0.0


def test_multiple_sources_summed_and_sorted_desc():
    a = _entry("a", channel="stp")
    b = _entry("b", channel="mystic")
    report = compute_attribution(
        [a, b], [_sell("a", 10, 110.0), _sell("b", 10, 90.0)], [])
    assert [s.channel for s in report.sources] == ["stp", "mystic"]  # +100 before -100
    assert report.grand_total == 0.0


def test_per_ticker_lines_grouped_by_ticker_and_instrument():
    nvda = _entry("a", channel="stp", ticker="NVDA")
    tsla = _entry("b", channel="stp", ticker="TSLA")
    report = compute_attribution(
        [nvda, tsla], [_sell("a", 10, 110.0), _sell("b", 10, 90.0)], [])
    s = report.sources[0]
    lines = {(l.ticker, l.instrument_type): l for l in s.by_ticker}
    assert lines[("NVDA", "equity")].realized == 100.0
    assert lines[("NVDA", "equity")].closed_lots == 1
    assert lines[("TSLA", "equity")].realized == -100.0


def test_open_option_lot_counted_and_costed_not_closed():
    # option with NO sells: open, $0 realized, cost basis = 2.0*1*100 = 200
    e = _entry("opt", channel="stp", ticker="AAPL", itype="option",
               fill_price=2.0, fill_qty=1)
    report = compute_attribution([e], [], [])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.open_options == 1
    assert s.open_option_cost == 200.0
    assert s.closed_lots == 0
    assert report.total_closed_lots == 0


def test_closed_lots_counted_at_report_level():
    a = _entry("a", channel="stp")
    b = _entry("b", channel="mystic")
    report = compute_attribution(
        [a, b], [_sell("a", 10, 110.0), _sell("b", 5, 90.0)], [])
    assert report.total_closed_lots == 2


def test_win_rate_and_avg_win_loss_and_extremes():
    # 3 closed lots in one source: +100, +40, -100
    a = _entry("a", ticker="NVDA")
    b = _entry("b", ticker="TSLA")
    c = _entry("c", ticker="AMD")
    report = compute_attribution(
        [a, b, c],
        [_sell("a", 10, 110.0), _sell("b", 4, 110.0), _sell("c", 10, 90.0)], [])
    s = report.sources[0]
    assert s.closed_lots == 3
    assert s.wins == 2
    assert s.losses == 1
    assert s.win_rate == 2 / 3
    assert s.avg_win == 70.0      # (100 + 40) / 2
    assert s.avg_loss == -100.0   # (-100) / 1
    assert s.best_lot == 100.0
    assert s.worst_lot == -100.0
    assert report.total_wins == 2
    assert report.win_rate == 2 / 3


def test_no_closed_lots_yields_zero_stats():
    e = _entry("opt", itype="option", fill_price=2.0, fill_qty=1)  # open option
    report = compute_attribution([e], [], [])
    s = report.sources[0]
    assert s.win_rate == 0.0
    assert s.avg_win == 0.0
    assert s.avg_loss == 0.0
    assert s.best_lot == 0.0
    assert s.worst_lot == 0.0


def test_zero_fill_exit_is_ignored():
    # a zero-fill follow-sell (sold_qty=0, NULL price) must not close the lot
    report = compute_attribution(
        [_entry("a")], [],
        [{"intent_id": "a", "sold_qty": 0, "sold_avg_price": None}])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert s.flags == []  # zero-fill is normal, not an anomaly


def test_sell_with_null_price_is_flagged_and_excluded():
    # sold_qty>0 but price NULL: cannot value -> exclude, flag, lot not closed
    report = compute_attribution(
        [_entry("a", ticker="NVDA")], [],
        [{"intent_id": "a", "sold_qty": 5, "sold_avg_price": None}])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert any("NVDA" in f and "NULL price" in f for f in s.flags)


def test_zero_cost_entry_with_sells_is_flagged_and_excluded():
    # fill_price 0.0 would fabricate a phantom gain -> exclude + flag
    e = _entry("a", ticker="AMD", fill_price=0.0, fill_qty=10)
    report = compute_attribution([e], [_sell("a", 10, 50.0)], [])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert any("AMD" in f and "fill_price" in f for f in s.flags)


def test_near_zero_lot_is_neither_win_nor_loss():
    # realized ~1e-12 (tiny float residue) must not count as a win or loss
    # buy 1 option @ 1.0; sell 1 @ 1.0 + 1e-14 → realized = (1e-14)*100 = 1e-12
    e = _entry("a", itype="option", fill_price=1.0, fill_qty=1)
    sell_price = 1.0 + 1e-14  # proceeds exceed cost by 1e-12 after 100x mult
    report = compute_attribution([e], [_sell("a", 1, sell_price)], [])
    s = report.sources[0]
    assert s.closed_lots == 1
    assert s.wins == 0
    assert s.losses == 0
