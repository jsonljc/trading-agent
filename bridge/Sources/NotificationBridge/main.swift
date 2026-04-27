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

let watchedChannels = ["mystic", "alerts", "trades", "wall-st-engine", "stock-talk-portfolio", "chat",
                       "yonezu", "pup-danny", "urkel", "gladiator", "graddox", "phat", "grid"]

let dedup = FingerprintDedup()
let socketEmitter = SocketEmitter(socketPath: socketPath)

let watcher = AXDiscordWatcher(
    bundleId: "com.hnc.Discord",
    watchedChannels: watchedChannels,
    socketPath: socketPath,
    logPath: logPath,
    dedup: dedup
)

let dbPoller = NotificationDBPoller(
    watchedChannels: watchedChannels,
    emitter: socketEmitter,
    dedup: dedup
)

dbPoller.start()
watcher.start()

RunLoop.main.run()
