# Monday RTH Live Smoke — finishes verification of fix/no-trades-may-8

This is the live-broker portion of Task 6 from
`docs/superpowers/plans/2026-05-10-no-trades-may-8-fixes.md`. The schema,
trader pattern, audit script, and Chrome extension fixes have all been
verified offline. This runbook completes the loop: an actual paper-account
order placed through IB Gateway from a synthetic signal.

## Pre-flight

- [ ] Branch `fix/no-trades-may-8` is checked out locally and merged or rebased current.
- [ ] Today is a US market trading day, time is between 09:30 and 15:30 ET (leave a 30-min buffer before close).
- [ ] IB Gateway is running on port 4002, paper-trading mode (`DU` account prefix).
- [ ] No critical positions are at risk if the smoke order fills (smoke uses a small notional on a liquid name).

## Step 1: Reload the Chrome extension

In Chrome: `chrome://extensions` → find the Discord capture extension → click reload (↻). Then reload any open Discord tabs so the new `extract.js` is active.

## Step 2: Restart the agent

```bash
bin/agent-stop && sleep 2 && bin/agent-start
sleep 5
bin/agent-status
```

Confirm the status shows the agent process running.

## Step 3: Verify IB connection in the log

```bash
grep -E "IBGateway|connect" logs/agent.log | tail -5
```

Expected: a line indicating successful connection to IB Gateway. If you see `IBGatewayUnavailable`, fix the gateway connection before continuing.

## Step 4: Inject a synthetic Pup Danny signal

Use a liquid, low-priced ticker for the smoke test:

```bash
venv/bin/python inject_event.py "OPEN ORCL test smoke verification 2026-05-11" \
  --channel pup-danny \
  --author "The Pup of Wall St"
```

Expected console output: `Injected: <event-id> | pup-danny | OPEN ORCL ...`

## Step 5: Watch the agent log for the pipeline trace

```bash
tail -f logs/agent.log
```

Expected sequence (within ~10 seconds):
- `MessageNormalizer / DesktopReader: success`
- `TraderRouter: success` with trader_handle=pup-danny (this is the NEW behavior — pre-fix this would skip with `no_trader_profile:The Pup of Wall St`)
- `TraderClassifier: success` with bucket=HIGH or LOW
- `EntrySkipGate: success` (or skip with bucket=SKIP, in which case rerun with a different message that's clearly an entry)
- `IdempotencyCheck: success`
- `TickerValidator: success`
- `TradeIntentWriter: created intent ...`
- `ChannelPolicyGuard / CooldownGuard / ExecutionEligibilityGuard: success`
- `RthEntryGuard: success` with session=rth
- `ReferencePriceCapture / SizingResolver / EquityContractBuilder / OrderSizer: success`
- `SharesMarketSubmitter: success` with `shares_intent_id, shares_fill_price, shares_fill_qty` updates

Press Ctrl-C to stop tailing once you see the SharesMarketSubmitter line.

## Step 6: Verify the trade intent has a broker order ref

```bash
sqlite3 data/trading_agent.db \
  "SELECT intent_id, ticker, conviction, instrument_type, execution_state, fill_price, fill_qty, broker_order_ref FROM trade_intents WHERE channel='pup-danny' AND ticker='ORCL' ORDER BY intent_created_at DESC LIMIT 5;"
```

Expected: most-recent row has `ticker=ORCL`, `instrument_type=equity`, `broker_order_ref` is **non-null** (proves the order reached IB Gateway), and `execution_state` is `filled` or `submitted`.

If `broker_order_ref` is null and there's a failure reason in the log, that's a downstream bug beyond this plan's four fixes — open a follow-up issue.

## Step 7: Final regression check — confirm no schema errors

```bash
grep -E "OperationalError|parent_intent_id|fill_qty" logs/agent.log | tail -20
```

Expected: zero hits. If any `OperationalError` appears, the schema migration didn't take effect on this DB; rerun the migration explicitly (see Task 2 of the plan).

## Step 8: Confirm Discord-extension empty-author rate has dropped

After the agent has been running for ~30 minutes during RTH and has captured fresh signals:

```bash
sqlite3 data/trading_agent.db \
  "SELECT channel, count(*) AS total, sum(CASE WHEN author='' THEN 1 ELSE 0 END) AS empty_authors FROM signal_events WHERE received_at >= '2026-05-11' GROUP BY channel ORDER BY 1;"
```

Expected: empty_authors should be 0 or near-zero on the new traffic. Pre-fix ratio was ~30% empty.

## Done

If all 8 steps pass, the no-trades-may-8 plan is complete end-to-end — squash-merge the branch and close the loop.
