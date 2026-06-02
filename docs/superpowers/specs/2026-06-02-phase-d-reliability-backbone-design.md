# Phase D — Reliability & Observability Backbone: Design & Plan

**Goal:** Make the agent survive crashes/restarts and surface failures instead of
dropping them silently. The schema already carries every column needed
(`execution_state`, `outbox_status`, `broker_order_ref`, `dlq_reason`,
`order_submitted_at`, the `dlq_intents` view); the gaps are *behavioral* — nothing
writes the write-ahead state, the reconciler is a logging stub, malformed socket
events vanish, and there is no kill switch or heartbeat.

**Branch:** `fix/no-trades-may-8`. TDD, one commit per task. Paper mode only.
Does NOT add any automated stop-loss / kill on P&L (the manual kill switch is an
operator emergency stop for NEW entries, not a risk exit — held positions are still
exited only by following the trader).

**Order-state machine (the spine of D1 + D4):**
```
TradeIntentWriter         execution_state=None      outbox_status='pending'
shares write-ahead        ='submitted'              ='dispatched'   (+broker_order_ref, order_submitted_at)
  on FILLED / partial     ='filled'                 ='confirmed'    (+fill_price, fill_qty, filled_at)
  on REJECTED             ='failed'                 ='failed'       (+dlq_reason)  -> dlq_intents view
  on zero-fill timeout    ='cancelled'              ='cancelled'    (+cancel_reason)
```
`get_pending_outbox` (IN 'pending','dispatched') then returns exactly the in-flight
orders the reconciler must check; terminal states drop out of it.

---

## D1 — Crash-recovery write-ahead + order-rejection DLQ (shares)

- `TradeIntentWriter`: set `outbox_status='pending'` on insert.
- `SharesMarketSubmitter`: after `place_order`, before `wait_fill`, write-ahead
  `execution_state='submitted'`, `outbox_status='dispatched'`,
  `broker_order_ref=str(trade.order.orderId)`, `order_submitted_at=now`.
- `update_fill` also sets `outbox_status='confirmed'`.
- `wait_fill` REJECTED → `execution_state='failed'`, `dlq_reason`,
  `outbox_status='failed'`; submitter returns a distinct `shares_rejected:` reason.
- zero-fill timeout → cancel residual, `execution_state='cancelled'`,
  `cancel_reason='fill_timeout'`, `outbox_status='cancelled'`.
- Distinct Telegram alert: `TelegramDigest.send_order_rejected_alert`, fired from
  `on_fail` when the reason marks a rejection (vs the generic error digest).

> **Scope note — shares vs options crash-recovery asymmetry (deliberate):** the
> full write-ahead state machine lives on the **shares** leg only. The options
> intent is written *post-fill* by design (it is the secondary convex sleeve), so
> there is no pre-`wait_fill` options row, and an options rejection has no intent
> to mark `failed`/`dlq_reason` — the shares leg is the sole DLQ producer. An
> options rejection does still emit a distinct `options_rejected:` reason that
> routes to the ORDER REJECTED alert, and the reconciler's orphan check (which
> matches `:options:` orderRefs) catches an options order placed-then-crashed.
> Giving the options leg a symmetric write-ahead is a deferred follow-up.

## D2 — Manual kill switch (sentinel file)

`KillSwitchGuard` skill at the FRONT of the phase2b chain: if a sentinel file
exists (path from policy, default `data/KILL`), return `skip` with reason
`kill_switch_engaged` so NEW entries halt instantly. Logged + (once) alerted.
Held positions and the trim ladder are unaffected — this only blocks new entries.

## D3 — SocketReader dead-letter on parse failure

Replace the silent `logger.exception` on a malformed event with: append the raw
line to a dead-letter file (`logs/bridge_deadletter.jsonl`), increment a counter,
and invoke an optional `on_parse_error` alert callback. The Chrome extension is the
SOLE capture path, so a parse failure is a real dropped signal and must be visible.

## D4 — Real ExecutionReconciler

On startup, every loop, and on reconnect: pull live IB state
(`gateway.get_open_orders()` + new `gateway.get_positions()`) and diff against
`get_pending_outbox()`:
- db `dispatched`/`submitted` intent whose `broker_order_ref` is NOT in live open
  orders AND not in positions → log + alert "order vanished while down" (manual
  review — never auto-resubmit).
- live IB open order with no matching db intent → log + alert "orphan broker order".
Keep the existing stuck-outbox logging. Configure a Master Client ID (or clientId 0
+ `reqAutoOpenOrders`) so prior-session orders are visible — documented as an
operator step; the diff logic is built and unit-tested with a fake gateway.

## D5 — Watchdog/heartbeat

`Heartbeat` task pinging a healthchecks.io-style URL (`policy.execution.heartbeat_url`,
default None = disabled) every N seconds from the main loop, so a crashed/slept bot
is detectable externally (a quiet day and a dead bot look identical today).

## D6 — DB integrity + off-machine backup

On startup: `PRAGMA integrity_check` (log + alert on non-'ok'); `VACUUM INTO` a
timestamped snapshot under a configurable backup dir.

---

## Deferred (documented, not built this pass — with rationale)

- **Concurrency dispatcher** (asyncio task per signal + bounded semaphore +
  per-ticker lock): real value but changes the main loop's serial guarantee and is
  hard to cover without integration tests; higher regression risk than the items
  above. Follow-up.
- **Latency waterfall** (per-stage timestamps + p50/p95/p99): observability nicety,
  lower urgency than crash-recovery.
- **`ib_insync` → `ib_async` migration**: ~import rename but touches every broker
  call; unmaintained-since-2024 is a slow-burn risk, not an outage risk. Do as an
  isolated branch with the full suite as the gate.
- **Phase E** (sell-following, P&L attribution, replay, classifier eval): separate
  spec(s).
