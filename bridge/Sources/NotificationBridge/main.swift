import Foundation

let socketPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "/tmp/trading_bridge.sock"

let discordBundleId = "com.hnc.Discord"
let watchedChannels = ["mystic", "alerts", "trades"]

let poller = NotificationPoller()
let emitter = SocketEmitter(socketPath: socketPath)

print("NotificationBridge started. Socket: \(socketPath)")

while true {
    let records = poller.pollNew()
    for record in records {
        guard record.appBundleId == discordBundleId else { continue }

        let channelName = record.subtitle
            .trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: "#", with: "")
            .lowercased()

        guard watchedChannels.contains(channelName) else { continue }

        let eventId = UUID().uuidString
        let event: [String: String] = [
            "event_id": eventId,
            "source": "discord_notification",
            "channel": channelName,
            "author": record.title,
            "trigger_preview": record.body,
            "received_at": ISO8601DateFormatter().string(from: Date()),
        ]

        emitter.emit(event)
        print("Emitted event: \(eventId) channel=\(channelName) preview=\(record.body.prefix(60))")
    }

    Thread.sleep(forTimeInterval: 0.5)
}
