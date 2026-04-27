import Foundation
import Darwin
import NotificationBridgeCore
setbuf(stdout, nil)  // unbuffered stdout so logs appear immediately

let socketPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1] : "/tmp/trading_bridge.sock"
let logPath = CommandLine.arguments.count > 2
    ? CommandLine.arguments[2] : "data/ax_events.log"

let dataDir = URL(fileURLWithPath: logPath).deletingLastPathComponent().path
try? FileManager.default.createDirectory(atPath: dataDir, withIntermediateDirectories: true)

let watcher = AXDiscordWatcher(
    bundleId: "com.hnc.Discord",
    watchedChannels: ["mystic", "alerts", "trades", "wall-st-engine", "stock-talk-portfolio", "chat",
                      "yonezu", "pup-danny", "urkel", "gladiator", "graddox", "phat", "grid"],
    socketPath: socketPath,
    logPath: logPath
)

// Click Discord notification banners so Discord navigates to the channel,
// then trigger an immediate reconcile sweep to capture the message.
let bannerClicker = NotificationBannerClicker(discordBundleId: "com.hnc.Discord") {
    watcher.triggerReconcile()
}
bannerClicker.start()

watcher.start()
