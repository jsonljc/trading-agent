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
