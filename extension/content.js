// Discord priority-channel full-text capture.
//
// Strategy:
//   1. Wait for the messages list to mount (Discord lazily loads it).
//   2. Snapshot existing visible messages into a "seen" set so we don't
//      emit the channel's history on first load.
//   3. MutationObserver fires for each new message; we extract and POST.

(function () {
  const FORWARDER_URL = "http://localhost:9876/signal";
  const MOUNT_TIMEOUT_MS = 30000;
  const POLL_INTERVAL_MS = 250;
  const POST_RETRIES = 3;
  const POST_BACKOFF_MS = 500;

  function log(...args) { console.log("[trading-agent-ext]", ...args); }
  function warn(...args) { console.warn("[trading-agent-ext]", ...args); }

  async function waitForMessagesList(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      // The messages-list container has a stable data-list-id under React.
      // Fall back to scanning for any chat-messages-* element's parent.
      const probe = document.querySelector('[id^="chat-messages-"]');
      if (probe && probe.parentElement) return probe.parentElement;
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

  async function init() {
    const { server_id, channel_id } = DiscordExtract.channelIdFromUrl(
      window.location.href);
    if (!channel_id) { log("not on a channel URL; bailing"); return; }

    const container = await waitForMessagesList(MOUNT_TIMEOUT_MS);
    if (!container) { warn("messages list never mounted"); return; }

    log("attached to messages list for channel", channel_id);

    const seen = new Set();
    container.querySelectorAll('[id^="chat-messages-"]').forEach(el => {
      seen.add(el.id);
    });
    log("snapshot:", seen.size, "existing messages");

    const observer = new MutationObserver(mutations => {
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
            const payload = { ...extracted, channel_id, server_id };
            postSignal(payload).then(ok => {
              if (ok) log("forwarded", extracted.message_id, extracted.content.slice(0, 60));
            });
          }
        }
      }
    });
    observer.observe(container, { childList: true, subtree: true });
  }

  // Re-run on URL change (Discord is a SPA — channel switches don't reload).
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      log("url changed; re-initialising");
      init();
    }
  }, 1000);

  init();
})();
