// Discord priority-channel full-text capture.
//
// Strategy:
//   1. Wait for the messages list to mount (Discord lazily loads it).
//   2. Snapshot existing visible messages into a "seen" set so we don't
//      emit the channel's history on first load.
//   3. MutationObserver fires for each new message; we extract and POST.
//   4. A resilience loop re-attaches the observer if its target node is
//      detached/replaced (see FAILURE MODE below), and a periodic per-channel
//      beacon proves to the watchdog that this capture is still alive.
//
// Pure decision helpers (shouldReattach / shouldBeacon / beaconBody) are
// exported on `DiscordCapture` so extension/test/ can unit-test them without a
// live Discord page; the runtime loops are skipped when __DISCORD_CAPTURE_TEST__
// is set so loading this file under a test harness has no side effects.

(function (root) {
  const FORWARDER_URL = "http://localhost:9876/signal";
  const BEACON_URL = "http://localhost:9876/beacon";
  const MOUNT_TIMEOUT_MS = 30000;
  const POLL_INTERVAL_MS = 250;
  const POST_RETRIES = 3;
  const POST_BACKOFF_MS = 500;
  const HEALTH_INTERVAL_MS = 5000;    // how often to verify the observer target
  const BEACON_INTERVAL_MS = 60000;   // per-channel liveness beacon cadence

  // State for the currently-active capture.
  let currentObserver = null;
  let currentContainer = null;
  let currentChannelId = "";
  let currentServerId = "";
  let seen = new Set();
  let initInFlight = false;

  function log(...args) { console.log("[trading-agent-ext]", ...args); }
  function warn(...args) { console.warn("[trading-agent-ext]", ...args); }

  // The messages-list container is the parent of any chat-messages-* element.
  function findMessagesContainer() {
    const probe = document.querySelector('[id^="chat-messages-"]');
    return probe && probe.parentElement ? probe.parentElement : null;
  }

  async function waitForMessagesList(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const container = findMessagesContainer();
      if (container) return container;
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }
    return null;
  }

  async function postSignal(payload) {
    let lastErr = null;
    for (let attempt = 0; attempt < POST_RETRIES; attempt++) {
      try {
        const resp = await fetch(FORWARDER_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          mode: "cors",
        });
        if (resp.ok || resp.status === 204) return true;
        lastErr = new Error("HTTP " + resp.status);
      } catch (e) {
        lastErr = e;
      }
      await new Promise(r => setTimeout(r, POST_BACKOFF_MS));
    }
    warn("Failed to forward signal after retries:", lastErr);
    return false;
  }

  // --- pure decision helpers (exported for tests) --------------------------

  // Re-attach when our observed node is gone from the live DOM (detached) OR a
  // different messages-list node has been mounted in its place (replaced).
  function shouldReattach(container, liveContainer) {
    const detached = !container || !container.isConnected;
    const replaced = !!liveContainer && liveContainer !== container;
    return detached || replaced;
  }

  // Only beacon when we have a channel AND the observed node is still attached.
  // A fresh beacon must mean capture is genuinely alive — not merely that the
  // tab is open with an orphaned observer.
  function shouldBeacon(channelId, container) {
    return !!channelId && !!container && container.isConnected === true;
  }

  function beaconBody(channelId) {
    return JSON.stringify({ channels: [channelId] });
  }

  // Liveness ping: tell the forwarder this tab is actively watching its channel.
  function sendBeacon() {
    if (!shouldBeacon(currentChannelId, currentContainer)) return;
    try {
      fetch(BEACON_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: beaconBody(currentChannelId),
        mode: "cors",
        keepalive: true,
      }).catch(() => { /* best-effort; watchdog handles a missed window */ });
    } catch (e) { /* ignore */ }
  }

  function handleMutations(mutations) {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof Element)) continue;
        // Added node may be the message itself or a wrapper containing one.
        const candidates = node.id && node.id.startsWith("chat-messages-")
          ? [node]
          : Array.from(node.querySelectorAll('[id^="chat-messages-"]'));
        for (const el of candidates) {
          if (seen.has(el.id)) continue;
          seen.add(el.id);
          const extracted = DiscordExtract.extractMessage(el);
          if (!extracted) continue;
          const payload = { ...extracted, channel_id: currentChannelId, server_id: currentServerId };
          postSignal(payload).then(ok => {
            if (ok) log("forwarded", extracted.message_id, extracted.content.slice(0, 60));
          });
        }
      }
    }
  }

  function teardown() {
    if (currentObserver) {
      try { currentObserver.disconnect(); } catch (e) { /* ignore */ }
    }
    currentObserver = null;
    currentContainer = null;
    currentChannelId = "";
    currentServerId = "";
  }

  async function init() {
    if (initInFlight) return;  // collapse overlapping (re)init attempts
    initInFlight = true;
    try {
      if (!DiscordExtract.channelIdFromUrl(window.location.href).channel_id) {
        log("not on a channel URL; bailing");
        teardown();
        return;
      }

      const container = await waitForMessagesList(MOUNT_TIMEOUT_MS);
      if (!container) { warn("messages list never mounted"); return; }

      // Re-resolve AFTER the await: the user may have switched channels while we
      // were waiting for the list to mount.
      const { server_id, channel_id } = DiscordExtract.channelIdFromUrl(window.location.href);
      if (!channel_id) { teardown(); return; }

      teardown();  // drop any prior observer before attaching a new one
      currentContainer = container;
      currentChannelId = channel_id;
      currentServerId = server_id;

      seen = new Set();
      container.querySelectorAll('[id^="chat-messages-"]').forEach(el => seen.add(el.id));
      log("attached to messages list for channel", channel_id, "— snapshot", seen.size);

      currentObserver = new MutationObserver(handleMutations);
      currentObserver.observe(container, { childList: true, subtree: true });
      sendBeacon();  // confirm liveness immediately on (re)attach
    } finally {
      initInFlight = false;
    }
  }

  // --- resilience loop -----------------------------------------------------
  //
  // FAILURE MODE THIS GUARDS AGAINST:
  // The MutationObserver is bound to the message-list node captured at mount.
  // Discord re-mounts that node on reconnect-after-sleep / websocket reconnect
  // (and on some lazy re-renders) WITHOUT changing location.href. When that
  // happens the observer is left watching a DETACHED node: no mutations fire,
  // new messages are silently dropped, yet the tab stays open and the URL is
  // unchanged — so URL-change-only re-init never runs and capture dies silently
  // (the exact bug this fix targets). We therefore also poll: if our observed
  // node is no longer connected to the DOM, or a different messages-list node
  // has been mounted in its place, we disconnect and re-init on a stable node.
  function startResilienceLoop() {
    let lastUrl = location.href;
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        log("url changed; re-initialising");
        init();
        return;
      }
      if (!DiscordExtract.channelIdFromUrl(location.href).channel_id) return;
      const live = findMessagesContainer();
      if (shouldReattach(currentContainer, live)) {
        warn("observer target detached/replaced; re-attaching");
        init();
      }
    }, HEALTH_INTERVAL_MS);
  }

  // Per-channel liveness beacon (watchdog reads these). Co-located with the
  // observer on purpose: it stops exactly when capture stops (tab discarded or
  // closed). A background service worker could keep beaconing while the DOM
  // capture was already dead, producing a false-healthy signal — so the beacon
  // deliberately lives in the content script.
  function startBeaconLoop() {
    setInterval(sendBeacon, BEACON_INTERVAL_MS);
  }

  // Exported for extension/test/ — pure, side-effect-free decision helpers.
  root.DiscordCapture = { shouldReattach, shouldBeacon, beaconBody };

  // Skip live wiring under the test harness so loading this file is inert.
  if (!root.__DISCORD_CAPTURE_TEST__) {
    startResilienceLoop();
    startBeaconLoop();
    init();
  }
})(typeof window !== "undefined" ? window : globalThis);
