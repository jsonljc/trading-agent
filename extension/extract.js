// Pure DOM extraction. Given a Discord message <li> element, returns a
// {message_id, author, content, timestamp} object, or null if the element
// doesn't look like a renderable message.
//
// Selectors are attribute-based ([id^=...], [class*=...]) because Discord
// rotates class hashes on every release.
(function (root) {
  function extractMessage(el) {
    if (!el || !el.id || !el.id.startsWith("chat-messages-")) return null;

    // chat-messages-<channelId>-<messageId>
    const parts = el.id.split("-");
    const message_id = parts[parts.length - 1];
    if (!message_id) return null;

    const contentEl = el.querySelector('[id^="message-content-"]');
    const content = contentEl ? contentEl.innerText : "";

    const usernameEl = el.querySelector('[class*="username"]');
    const author = usernameEl ? usernameEl.innerText.trim() : "";

    const timeEl = el.querySelector("time");
    const timestamp = timeEl ? timeEl.getAttribute("datetime") : "";

    return { message_id, author, content, timestamp };
  }

  function channelIdFromUrl(url) {
    // https://discord.com/channels/<server_id>/<channel_id>
    const m = url.match(/\/channels\/(\d+)\/(\d+)/);
    if (!m) return { server_id: "", channel_id: "" };
    return { server_id: m[1], channel_id: m[2] };
  }

  root.DiscordExtract = { extractMessage, channelIdFromUrl };
})(typeof window !== "undefined" ? window : globalThis);
