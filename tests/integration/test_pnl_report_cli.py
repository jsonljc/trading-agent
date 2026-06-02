# tests/integration/test_pnl_report_cli.py
# NOTE: these are SYNC tests. bin/pnl_report.py is synchronous and calls
# asyncio.run() internally for --telegram; running them under pytest-asyncio's
# event loop would raise "asyncio.run() cannot be called from a running event
# loop". So the (async) DB seeding is driven via asyncio.run() in the fixture,
# and the tests themselves are plain `def`.
import asyncio
import pytest
from infra.storage.db import get_connection
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.position_exit_store import PositionExitStore
from infra.storage.trim_ladder_store import TrimLadderStore
import bin.pnl_report as cli


async def _seed(db_path):
    """Two sources. stp: NVDA closed +100. mystic: TSLA closed -50.
    Plus an stp open option (no exit path) and a zero-fill exit (ignored)."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    exits = PositionExitStore(conn)

    async def filled(intent_id, channel, ticker, itype, price, qty):
        await intents.insert({
            "intent_id": intent_id, "event_id": "e", "channel": channel,
            "ticker": ticker, "side": "long", "instrument_type": itype,
            "conviction": "high", "fill_price": price, "fill_qty": qty,
            "execution_state": "filled", "outbox_status": "confirmed",
            "policy_state": "approved", "signal_received_at": "2026-05-01T00:00:00+00:00",
            "intent_created_at": "2026-05-01T00:00:00+00:00",
            "filled_at": "2026-05-01T14:00:00+00:00",
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00"})

    await filled("nvda", "stp", "NVDA", "equity", 100.0, 10)
    await filled("tsla", "mystic", "TSLA", "equity", 100.0, 10)
    await filled("aapl", "stp", "AAPL", "option", 2.0, 1)  # open option

    await exits.record_exit(fingerprint="f1", event_id="e", intent_id="nvda",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=110.0,
                            broker_order_ref="r1", reason="follow_sell")
    await exits.record_exit(fingerprint="f2", event_id="e", intent_id="tsla",
                            channel="mystic", ticker="TSLA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=95.0,
                            broker_order_ref="r2", reason="follow_sell")
    # zero-fill exit must be ignored
    await exits.record_exit(fingerprint="f3", event_id="e", intent_id="nvda",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=1, sold_qty=0, sold_avg_price=None,
                            broker_order_ref=None, reason="follow_sell")
    await conn.close()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    asyncio.run(_seed(db_path))
    return db_path


def test_cli_prints_per_source_totals(seeded_db, capsys):
    rc = cli.main(["--db", seeded_db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stp" in out and "mystic" in out
    assert "+100.00" in out      # NVDA realized
    assert "-50.00" in out       # TSLA realized
    assert "AAPL" in out         # open option surfaced
    assert "no exit path" in out


def test_cli_channel_filter(seeded_db, capsys):
    cli.main(["--db", seeded_db, "--channel", "stp"])
    out = capsys.readouterr().out
    assert "stp" in out
    assert "mystic" not in out


def test_cli_missing_db_exits_nonzero(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_render_telegram_summary_is_compact_html():
    from agent.pnl_attribution import (
        AttributionReport, SourcePnl, InstrumentBreakdown)
    s = SourcePnl(channel="stp", realized=100.0, closed_lots=1, wins=1,
                  by_instrument=InstrumentBreakdown(equity=100.0))
    report = AttributionReport(sources=[s], grand_total=100.0,
                               total_closed_lots=1, total_wins=1)
    html = cli.render_telegram(report)
    assert "stp" in html
    assert "+100.00" in html
    assert "<b>" in html  # uses HTML parse_mode markup


def test_cli_tolerates_missing_sell_tables(tmp_path, capsys):
    """A legacy DB with only trade_intents (no trade_intent_trims, no
    position_exits) must not crash; the report renders with open/zero-realized
    rows and exits 0."""
    import sqlite3 as _sqlite3
    bare_db = str(tmp_path / "bare.db")
    conn = _sqlite3.connect(bare_db)
    conn.execute(
        "CREATE TABLE trade_intents ("
        "  intent_id TEXT PRIMARY KEY,"
        "  channel TEXT,"
        "  ticker TEXT,"
        "  instrument_type TEXT,"
        "  fill_price REAL,"
        "  fill_qty REAL,"
        "  execution_state TEXT,"
        "  filled_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO trade_intents VALUES (?,?,?,?,?,?,?,?)",
        ("nvda1", "stp", "NVDA", "equity", 100.0, 10,
         "filled", "2026-05-01T14:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", bare_db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stp" in out


# ---------------------------------------------------------------------------
# Finding 1: trim-ladder coverage + window-filter coverage
# ---------------------------------------------------------------------------

async def _seed_trim(db_path):
    """stp NVDA: bought 10 @ 100, trim rung 1 fired — sold 3 @ 110."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    trims = TrimLadderStore(conn)

    await intents.insert({
        "intent_id": "nvda-t", "event_id": "e", "channel": "stp",
        "ticker": "NVDA", "side": "long", "instrument_type": "equity",
        "conviction": "high", "fill_price": 100.0, "fill_qty": 10,
        "execution_state": "filled", "outbox_status": "confirmed",
        "policy_state": "approved",
        "signal_received_at": "2026-05-01T00:00:00+00:00",
        "intent_created_at": "2026-05-01T00:00:00+00:00",
        "filled_at": "2026-05-01T14:00:00+00:00",
        "created_at": "2026-05-01T00:00:00+00:00",
        "updated_at": "2026-05-01T00:00:00+00:00",
    })
    await trims.arm("nvda-t", rungs=[(1, 0.10, 0.30)],
                    armed_at="2026-05-01T14:00:00+00:00")
    await trims.record_fire(intent_id="nvda-t", rung=1,
                            fired_at="2026-05-02T10:00:00+00:00",
                            fire_price=110.0, sold_qty=3, sold_avg_price=110.0,
                            broker_order_ref="r1")
    await conn.close()


@pytest.fixture
def trim_db(tmp_path):
    db_path = str(tmp_path / "trim.db")
    asyncio.run(_seed_trim(db_path))
    return db_path


def test_cli_includes_trim_proceeds(trim_db, capsys):
    """A fired trim row contributes its realized proceeds to the report."""
    rc = cli.main(["--db", trim_db])
    out = capsys.readouterr().out
    assert rc == 0
    # bought 10 @ 100, trim sold 3 @ 110 → realized = (3×110 − 3×100) = +30
    assert "+30.00" in out
    assert "stp" in out


# ---------------------------------------------------------------------------

async def _seed_window(db_path):
    """One entry (NVDA stp), two trims with different fired_at dates.
    Also a second entry (TSLA stp) whose only trim is pre-cutoff."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    trims = TrimLadderStore(conn)

    base = {
        "event_id": "e", "channel": "stp", "side": "long",
        "instrument_type": "equity", "conviction": "high",
        "execution_state": "filled", "outbox_status": "confirmed",
        "policy_state": "approved",
        "signal_received_at": "2026-05-01T00:00:00+00:00",
        "intent_created_at": "2026-05-01T00:00:00+00:00",
        "created_at": "2026-05-01T00:00:00+00:00",
        "updated_at": "2026-05-01T00:00:00+00:00",
    }

    # NVDA: two rungs; rung 1 fires BEFORE cutoff, rung 2 fires ON cutoff
    await intents.insert({**base, "intent_id": "nvda-w", "ticker": "NVDA",
                          "fill_price": 100.0, "fill_qty": 10,
                          "filled_at": "2026-05-01T14:00:00+00:00"})
    await trims.arm("nvda-w", rungs=[(1, 0.10, 0.40), (2, 0.20, 0.30)],
                    armed_at="2026-05-01T14:00:00+00:00")
    # rung 1: before cutoff 2026-05-10 → sold 4 @ 120 → +80 (pre-window)
    await trims.record_fire(intent_id="nvda-w", rung=1,
                            fired_at="2026-05-05T10:00:00+00:00",
                            fire_price=120.0, sold_qty=4, sold_avg_price=120.0,
                            broker_order_ref="r1")
    # rung 2: on cutoff 2026-05-10 → sold 3 @ 130 → +90 (in-window)
    await trims.record_fire(intent_id="nvda-w", rung=2,
                            fired_at="2026-05-10T10:00:00+00:00",
                            fire_price=130.0, sold_qty=3, sold_avg_price=130.0,
                            broker_order_ref="r2")

    # TSLA: one rung fires entirely before cutoff → must not appear under --since-sell
    await intents.insert({**base, "intent_id": "tsla-w", "ticker": "TSLA",
                          "fill_price": 50.0, "fill_qty": 10,
                          "filled_at": "2026-04-01T14:00:00+00:00"})
    await trims.arm("tsla-w", rungs=[(1, 0.10, 0.50)],
                    armed_at="2026-04-01T14:00:00+00:00")
    # fires before cutoff
    await trims.record_fire(intent_id="tsla-w", rung=1,
                            fired_at="2026-04-15T10:00:00+00:00",
                            fire_price=60.0, sold_qty=5, sold_avg_price=60.0,
                            broker_order_ref="r3")
    await conn.close()


@pytest.fixture
def window_db(tmp_path):
    db_path = str(tmp_path / "window.db")
    asyncio.run(_seed_window(db_path))
    return db_path


def test_cli_since_sell_windows_realized(window_db, capsys):
    """--since-sell cutoff: only in-window sells contribute to realized.
    NVDA rung1 fired 2026-05-05 (pre-window), rung2 fired 2026-05-10 (in-window).
    With --since-sell 2026-05-10: realized = (3×130 − 3×100) = +90."""
    rc = cli.main(["--db", window_db, "--since-sell", "2026-05-10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "+90.00" in out
    # pre-window rung1 +80 must NOT be counted
    assert "+170.00" not in out


def test_cli_since_sell_drops_lots_with_no_in_window_sell(window_db, capsys):
    """--since-sell drops lots that have no sell on/after the cutoff.
    TSLA's only trim fired 2026-04-15, which is before 2026-05-10; it must not appear."""
    rc = cli.main(["--db", window_db, "--since-sell", "2026-05-10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TSLA" not in out


# ---------------------------------------------------------------------------

async def _seed_entry_dates(db_path):
    """Two entries with different filled_at: AMD filled 2026-04-01, NVDA filled 2026-05-15."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    exits = PositionExitStore(conn)

    base = {
        "event_id": "e", "side": "long", "instrument_type": "equity",
        "conviction": "high", "execution_state": "filled",
        "outbox_status": "confirmed", "policy_state": "approved",
        "signal_received_at": "2026-04-01T00:00:00+00:00",
        "intent_created_at": "2026-04-01T00:00:00+00:00",
        "created_at": "2026-04-01T00:00:00+00:00",
        "updated_at": "2026-04-01T00:00:00+00:00",
    }

    # AMD: filled 2026-04-01 (before cutoff 2026-05-01)
    await intents.insert({**base, "intent_id": "amd-d", "channel": "stp",
                          "ticker": "AMD", "fill_price": 80.0, "fill_qty": 10,
                          "filled_at": "2026-04-01T14:00:00+00:00"})
    await exits.record_exit(fingerprint="fa", event_id="e", intent_id="amd-d",
                            channel="stp", ticker="AMD", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=90.0,
                            broker_order_ref="r1", reason="follow_sell")

    # NVDA: filled 2026-05-15 (on/after cutoff 2026-05-01)
    await intents.insert({**base, "intent_id": "nvda-d", "channel": "stp",
                          "ticker": "NVDA", "fill_price": 100.0, "fill_qty": 10,
                          "filled_at": "2026-05-15T14:00:00+00:00"})
    await exits.record_exit(fingerprint="fn", event_id="e", intent_id="nvda-d",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=110.0,
                            broker_order_ref="r2", reason="follow_sell")
    await conn.close()


@pytest.fixture
def entry_date_db(tmp_path):
    db_path = str(tmp_path / "entrydate.db")
    asyncio.run(_seed_entry_dates(db_path))
    return db_path


def test_cli_since_entry_filters_by_fill_date(entry_date_db, capsys):
    """--since-entry includes only lots whose filled_at >= cutoff.
    AMD filled 2026-04-01 (excluded), NVDA filled 2026-05-15 (included)."""
    rc = cli.main(["--db", entry_date_db, "--since-entry", "2026-05-01"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NVDA" in out      # in-window lot appears
    assert "AMD" not in out   # pre-cutoff lot excluded


# ---------------------------------------------------------------------------


def test_render_table_wins_only_source_shows_dash_for_avg_loss():
    """A source with only wins must show '-' for AvgLoss, not '+0.00'."""
    from agent.pnl_attribution import (
        AttributionReport, SourcePnl, InstrumentBreakdown)
    s = SourcePnl(channel="stp", realized=100.0, closed_lots=1,
                  wins=1, losses=0, avg_win=100.0, avg_loss=0.0,
                  by_instrument=InstrumentBreakdown(equity=100.0))
    report = AttributionReport(sources=[s], grand_total=100.0,
                               total_closed_lots=1, total_wins=1)
    table = cli.render_table(report)
    # wins-only: AvgLoss column must be a right-aligned dash, not '+0.00'
    assert "+0.00" not in table.split("\n")[2]   # data row, not header/footer
    # The dash must appear in the AvgLoss position
    data_row = [ln for ln in table.splitlines() if "stp" in ln][0]
    assert "-" in data_row


def test_render_table_losses_only_source_shows_dash_for_avg_win():
    """A source with only losses must show '-' for AvgWin, not '+0.00'."""
    from agent.pnl_attribution import (
        AttributionReport, SourcePnl, InstrumentBreakdown)
    s = SourcePnl(channel="stp", realized=-100.0, closed_lots=1,
                  wins=0, losses=1, avg_win=0.0, avg_loss=-100.0,
                  by_instrument=InstrumentBreakdown(equity=-100.0))
    report = AttributionReport(sources=[s], grand_total=-100.0,
                               total_closed_lots=1, total_wins=0)
    table = cli.render_table(report)
    data_row = [ln for ln in table.splitlines() if "stp" in ln][0]
    # AvgWin must be a dash, not '+0.00'
    assert "+0.00" not in data_row


def test_telegram_flag_sends_summary(seeded_db, capsys, monkeypatch):
    sent = []

    class FakePolicy:
        class telegram:
            bot_token = "x"
            chat_id = "y"

    monkeypatch.setattr(cli, "load_policy", lambda path: FakePolicy())

    class FakeClient:
        def __init__(self, token, chat_id):
            pass
        async def send_message(self, text):
            sent.append(text)

    monkeypatch.setattr(cli, "TelegramClient", FakeClient)
    rc = cli.main(["--db", seeded_db, "--telegram"])
    assert rc == 0
    assert len(sent) == 1
    assert "stp" in sent[0]
    # terminal table still printed
    assert "stp" in capsys.readouterr().out
