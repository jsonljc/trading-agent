# agent/pnl_attribution.py
"""Pure realized-P&L attribution. No DB, no I/O.

Realized P&L is per-lot: each filled entry intent is one lot, and every sell
(trim-ladder fire or follow-sell) is already keyed to that entry's intent_id,
so there is no FIFO/average-cost matching. See
docs/superpowers/specs/2026-06-02-per-source-pnl-attribution-design.md.

Row-shape contract (accessed by key; dict / sqlite3.Row / aiosqlite.Row all work):
  entry: intent_id, channel, ticker, instrument_type, fill_price, fill_qty
  trim:  intent_id, sold_qty, sold_avg_price
  exit:  intent_id, sold_qty, sold_avg_price
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field

OPTION_MULTIPLIER = 100
EPS = 1e-9  # float-zero band: realized in (-EPS, EPS) counts neither win nor loss


@dataclass
class TickerLine:
    ticker: str
    instrument_type: str  # 'equity' | 'option'
    realized: float
    closed_lots: int


@dataclass
class InstrumentBreakdown:
    equity: float = 0.0
    option: float = 0.0


@dataclass
class SourcePnl:
    channel: str
    realized: float = 0.0
    closed_lots: int = 0
    wins: int = 0
    losses: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_lot: float = 0.0
    worst_lot: float = 0.0
    by_instrument: InstrumentBreakdown = field(default_factory=InstrumentBreakdown)
    by_ticker: list[TickerLine] = field(default_factory=list)
    open_options: int = 0
    open_option_cost: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed_lots if self.closed_lots else 0.0


@dataclass
class AttributionReport:
    sources: list[SourcePnl] = field(default_factory=list)
    grand_total: float = 0.0
    total_closed_lots: int = 0
    total_wins: int = 0

    @property
    def win_rate(self) -> float:
        return self.total_wins / self.total_closed_lots if self.total_closed_lots else 0.0


def _valid_sells(rows):
    """(sold_qty, sold_avg_price) for real sales only. Skips zero-fill
    (sold_qty<=0) and NULL-price rows; returns sell_anomaly=True if a row had
    sold_qty>0 but a NULL price (a real sale at an unknown price)."""
    out, sell_anomaly = [], False
    for r in rows:
        q, p = r["sold_qty"], r["sold_avg_price"]
        if q is None or q <= 0:
            continue
        if p is None:
            sell_anomaly = True
            continue
        out.append((int(q), float(p)))
    return out, sell_anomaly


def compute_attribution(entries, trims, exits) -> AttributionReport:
    trims_by = defaultdict(list)
    for t in trims:
        trims_by[t["intent_id"]].append(t)
    exits_by = defaultdict(list)
    for e in exits:
        exits_by[e["intent_id"]].append(e)

    by_channel: dict[str, SourcePnl] = {}
    # (channel, ticker, instrument_type) -> [realized, closed_lots]
    ticker_acc: dict[tuple, list] = defaultdict(lambda: [0.0, 0])

    def src(ch: str) -> SourcePnl:
        if ch not in by_channel:
            by_channel[ch] = SourcePnl(channel=ch)
        return by_channel[ch]

    closed_realized: dict[str, list] = defaultdict(list)

    for entry in entries:
        iid = entry["intent_id"]
        channel = entry["channel"] or "(unknown)"
        ticker = entry["ticker"]
        itype = entry["instrument_type"]
        fill_price = entry["fill_price"]
        fill_qty = entry["fill_qty"] or 0
        sells, sell_anomaly = _valid_sells(trims_by.get(iid, []) + exits_by.get(iid, []))

        s = src(channel)
        anomalous = False
        if sells and (fill_price is None or fill_price <= 0):
            s.flags.append(f"{ticker}: fill_price<=0 with sells — lot excluded")
            anomalous = True
        if sell_anomaly:
            s.flags.append(f"{ticker}: sell with NULL price — excluded")

        if anomalous:
            continue  # excluded from all totals/stats; no by_ticker line

        sold_total = sum(q for q, _ in sells)
        proceeds = sum(q * p for q, p in sells)
        mult = OPTION_MULTIPLIER if itype == "option" else 1
        realized = (proceeds - sold_total * (fill_price or 0.0)) * mult
        is_closed = sold_total > 0

        s.realized += realized
        if itype == "option":
            s.by_instrument.option += realized
        else:
            s.by_instrument.equity += realized

        key = (channel, ticker, itype)
        ticker_acc[key][0] += realized
        if is_closed:
            s.closed_lots += 1
            closed_realized[channel].append(realized)
            if realized > EPS:
                s.wins += 1
            elif realized < -EPS:
                s.losses += 1
            ticker_acc[key][1] += 1
        elif itype == "option":
            s.open_options += 1
            s.open_option_cost += (fill_price or 0.0) * fill_qty * OPTION_MULTIPLIER

    for (ch, ticker, itype), (rl, cl) in ticker_acc.items():
        by_channel[ch].by_ticker.append(TickerLine(ticker, itype, rl, cl))
    for s in by_channel.values():
        s.by_ticker.sort(key=lambda l: l.realized, reverse=True)

    for ch, s in by_channel.items():
        cr = closed_realized[ch]
        wins = [r for r in cr if r > EPS]
        losses = [r for r in cr if r < -EPS]
        s.avg_win = sum(wins) / len(wins) if wins else 0.0
        s.avg_loss = sum(losses) / len(losses) if losses else 0.0
        s.best_lot = max(cr) if cr else 0.0
        s.worst_lot = min(cr) if cr else 0.0

    sources = sorted(by_channel.values(), key=lambda s: s.realized, reverse=True)
    grand = sum(s.realized for s in sources)
    total_closed = sum(s.closed_lots for s in sources)
    total_wins = sum(s.wins for s in sources)
    return AttributionReport(sources=sources, grand_total=grand,
                             total_closed_lots=total_closed, total_wins=total_wins)
