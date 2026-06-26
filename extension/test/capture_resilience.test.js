// Node-runnable unit test for the Part B re-attach / beacon decision logic in
// content.js (the observer-resilience + per-channel-liveness fix).
//
//   node extension/test/capture_resilience.test.js   # exits non-zero on failure
//
// content.js is loaded with __DISCORD_CAPTURE_TEST__ set, so its runtime loops
// (setInterval / fetch / init) are skipped and only the pure decision helpers
// it exports on DiscordCapture are exercised. This lets us cover the hard part
// (when to re-attach / when a beacon is valid) without driving a live Discord
// page or a MutationObserver.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const extDir = path.resolve(__dirname, "..");
const sandbox = { console, __DISCORD_CAPTURE_TEST__: true };
vm.createContext(sandbox);

// Same load order as manifest.json: extract.js then content.js.
for (const f of ["extract.js", "content.js"]) {
  vm.runInContext(fs.readFileSync(path.join(extDir, f), "utf8"), sandbox, { filename: f });
}

const { shouldReattach, shouldBeacon, beaconBody } = sandbox.DiscordCapture;

let failures = 0;
function check(name, cond) {
  const ok = !!cond;
  if (!ok) failures++;
  console.log((ok ? "PASS  " : "FAIL  ") + name);
}

const connected = { isConnected: true };
const detached = { isConnected: false };
const otherConnected = { isConnected: true };

// shouldReattach: the core silent-death detector.
check("healthy: same connected node -> no reattach",
  shouldReattach(connected, connected) === false);
check("detached: observed node fell out of the DOM -> reattach",
  shouldReattach(detached, otherConnected) === true);
check("replaced: Discord re-mounted a different list node -> reattach",
  shouldReattach(connected, otherConnected) === true);
check("null current container (not yet attached) -> reattach",
  shouldReattach(null, connected) === true);
check("connected but no live container resolved this tick -> no reattach",
  shouldReattach(connected, null) === false);

// shouldBeacon: a beacon must reflect *live* capture, not just an open tab.
check("beacon when channel present and node connected",
  shouldBeacon("123", connected) === true);
check("no beacon when node detached (orphaned observer)",
  shouldBeacon("123", detached) === false);
check("no beacon without a channel",
  shouldBeacon("", connected) === false);
check("no beacon when container is null",
  shouldBeacon("123", null) === false);

// beaconBody: forwarder's beacon_channel_ids() reads {"channels": [...]}.
check("beacon body shape matches forwarder contract",
  beaconBody("123") === JSON.stringify({ channels: ["123"] }));

// Loading under the test flag must be inert (only the exports appear).
check("test-flag load exported helpers without starting runtime loops",
  typeof sandbox.DiscordCapture === "object" &&
  typeof sandbox.DiscordExtract === "object");

if (failures) {
  console.error(`\n${failures} JS check(s) FAILED`);
  process.exit(1);
}
console.log("\nAll JS checks passed");
