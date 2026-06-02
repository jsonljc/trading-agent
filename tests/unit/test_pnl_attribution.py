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
