# Live-trading readiness gate

Authored: 2026-05-12 after the May 11 paper-day audit. Before switching the IB
Gateway from paper (port 4002, account `DU*`) to live, every item below must
be ticked off and dated.

## Why this exists

The May 11 paper session passed surface-level (most trades filled) but
hid two silent failures that would have cost real money:

1. **IB Gateway died at 19:45 ET**, reconnect loop gave up after 2.75 min, then
   every subsequent fired signal was silently dropped — including a HIGH
   conviction ADEA fire from mystic. We had no Telegram alert.
2. **Recap and past-tense messages fired as new entries.** `"Pushing $P, this
   was a timely upsize"` produced a trade; same for `LUMN` net-neutral roll,
   `AVAV` watchlist commentary, and four COIN recaps after one real entry.

Both classes of failure were patched on 2026-05-12 but have not yet run
against a real session. **Do not move to live before the gate below passes.**

## Pre-flight (must hold for the entire qualifying paper day)

- [ ] **Reconnect-never-gives-up code is exercised against a real IB Gateway
      restart during market hours.** Kill the gateway process for >5 minutes
      and confirm: (a) Telegram alert fires, (b) reconnect succeeds when the
      process comes back, (c) post-reconnect signals still flow.
- [ ] **Missed-signal alert fires** when a HIGH/LOW classification cannot reach
      the broker. Reproduce by killing the gateway, posting a test signal,
      verifying the `⚠️ MISSED SIGNAL` Telegram message arrives.
- [ ] **One full RTH paper session with zero unexpected trades.** Every
      `trade_intents` row must trace back to a message that is an actual
      announcement of a fresh entry by the trader — not a recap, past-tense
      brag, net-neutral roll, or watchlist setup. Audit by joining
      `classification_log` ↔ `trade_intents` and reading each `msg_text`.
- [ ] **Zero `⚠️ MISSED SIGNAL` alerts on that same session.**
- [ ] **Cross-channel deduplication audit.** Confirm that captures from
      `stock-talk-portfolio` vs `stocktalkweekly` (and `wall-st-engine` vs
      `wallstengine`) no longer produce parallel `trade_intents` rows for the
      same underlying Discord post. Currently watched_channels is single-key
      per trader; the channel-name drift in old data must not recur in new
      data.
- [ ] **`LiveTradingBlocked` audit.** Every order-submitting path
      (`SharesMarketSubmitter`, `OptionsMarketSubmitter`, any future submitter)
      must go through `IBGateway.place_order` so that an account-prefix /
      port mismatch raises `LiveTradingBlocked` before any IB call. Trace each
      submission code path and tick this only after every path is verified.

## Day-of cutover (apply when flipping to live)

- [ ] **Sizing reduced 4-5×.** `config/policy.yaml` `execution.sizing`
      per-channel values divided by at least 4 for the first live week.
      Document the pre-cut numbers in the commit message so you can revert.
- [ ] **Daily deployed-capital cap.** A hard ceiling — independent of
      per-trade sizing — that halts new orders for the day once exceeded.
      This is a *new* skill (`DailyCapGuard` or similar); not currently in
      the chain. Build and test on paper first.
- [ ] **Margin multiplier reset.** `execution.margin_multiplier` is currently
      `2.0` (paper-friendly). Confirm what value you want live and decide
      consciously — do not inherit the paper value by accident.
- [ ] **IB Gateway port change.** Switch `ib_gateway.port` from `4002` (paper)
      to `7496` (live TWS) or appropriate live port. Verify the
      `paper_account_prefixes` list still matches what the live account
      starts with so the `LiveTradingBlocked` guard cuts the right way.
- [ ] **Telegram chat verified.** Confirm `chat_id` is correct and you can
      receive messages on the live cutover day (no DND, no broken bot).

## First live week — additional safeguards

- [ ] **Daily end-of-day reconciliation.** Manually compare every filled
      `trade_intents` row against the IB account statement. Discrepancies
      get investigated same-day, not deferred.
- [ ] **Kill switch identified.** Know the single command/action that stops
      all new orders immediately (likely: stop the agent process, or set
      every `auto_execute: false` and SIGHUP). Time how long it takes from
      "I want to stop" to "no more orders being placed."

## Notes

- This list is a *minimum* gate, not a complete risk model. Position sizing,
  drawdown limits, sector concentration, options-vs-shares ratio — all
  things to think about that are NOT covered here.
- Items are dated `[ ]` when complete, e.g. `[x] 2026-05-19`.
