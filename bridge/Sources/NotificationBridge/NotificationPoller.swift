import Foundation
import SQLite3

public struct NotificationRecord {
    public let recId: Int64
    public let deliveredDate: Double
    public let appBundleId: String
    public let title: String
    public let subtitle: String
    public let body: String

    public init(recId: Int64, deliveredDate: Double, appBundleId: String, title: String, subtitle: String, body: String) {
        self.recId = recId
        self.deliveredDate = deliveredDate
        self.appBundleId = appBundleId
        self.title = title
        self.subtitle = subtitle
        self.body = body
    }
}

/// Not thread-safe. Call `start()` once from the main thread; the timer
/// invokes `tick()` on the same run loop. Do not call `pollNew()` or
/// `process(_:)` concurrently from other threads.
public final class NotificationDBPoller {
    private let dbPath: String
    private let watchedChannels: Set<String>
    private let emitter: SignalEmitter?
    private let dedup: FingerprintDedup
    private var lastSeenId: Int64
    private var timer: Timer?

    public init(dbPath: String? = nil,
                watchedChannels: [String],
                emitter: SignalEmitter?,
                dedup: FingerprintDedup,
                startingRecId: Int64? = nil) {
        if let dbPath = dbPath {
            self.dbPath = dbPath
        } else {
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            self.dbPath = "\(home)/Library/Application Support/com.apple.notificationcenter/db2/db"
        }
        self.watchedChannels = Set(watchedChannels)
        self.emitter = emitter
        self.dedup = dedup
        self.lastSeenId = startingRecId ?? Self.currentMaxRecId(dbPath: self.dbPath)
    }

    public func start() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.tick()
        }
        print("NotificationDBPoller running. db=\(dbPath) channels=\(watchedChannels.sorted()) startingRecId=\(lastSeenId)")
    }

    public func tick() {
        for record in pollNew() {
            process(record)
        }
    }

    /// Reads new rows from the notification DB. Public for testability.
    public func pollNew() -> [NotificationRecord] {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            return []
        }
        defer { sqlite3_close(db) }

        let sql = "SELECT rec_id, data, delivered_date FROM record WHERE rec_id > ? ORDER BY rec_id ASC LIMIT 20"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }

        sqlite3_bind_int64(stmt, 1, lastSeenId)

        var results: [NotificationRecord] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let recId = sqlite3_column_int64(stmt, 0)
            let deliveredDate = sqlite3_column_double(stmt, 2)
            guard let blobPtr = sqlite3_column_blob(stmt, 1) else { continue }
            let blobLen = sqlite3_column_bytes(stmt, 1)
            let data = Data(bytes: blobPtr, count: Int(blobLen))
            if let record = Self.decodeRecord(recId: recId, deliveredDate: deliveredDate, data: data) {
                results.append(record)
            }
            lastSeenId = max(lastSeenId, recId)
        }
        return results
    }

    /// Filter, parse, dedup, emit. Public for testability.
    public func process(_ record: NotificationRecord) {
        guard record.appBundleId == "com.hnc.Discord" else { return }
        guard let channel = ChannelNameParser.parse(record.subtitle) else { return }
        guard watchedChannels.contains(channel) else { return }
        let parsed = MessageBodyParser.parse(record.body)
        let fp = FingerprintDedup.make(channel: channel, author: parsed.author, body: parsed.message)
        guard dedup.markSeen(fp) else { return }
        let event: [String: String] = [
            "event_id": UUID().uuidString,
            "source": "notif_db",
            "channel": channel,
            "author": parsed.author,
            "trigger_preview": parsed.message,
            "received_at": ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: record.deliveredDate + 978307200)),
        ]
        emitter?.emit(event)
    }

    // MARK: - Static helpers

    private static func currentMaxRecId(dbPath: String) -> Int64 {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return 0 }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COALESCE(MAX(rec_id), 0) FROM record", -1, &stmt, nil) == SQLITE_OK else { return 0 }
        defer { sqlite3_finalize(stmt) }
        if sqlite3_step(stmt) == SQLITE_ROW {
            return sqlite3_column_int64(stmt, 0)
        }
        return 0
    }

    private static func decodeRecord(recId: Int64, deliveredDate: Double, data: Data) -> NotificationRecord? {
        guard let decoded = try? NSKeyedUnarchiver.unarchiveTopLevelObjectWithData(data),
              let dict = decoded as? NSDictionary,
              let req = dict["req"] as? NSDictionary else { return nil }
        let bundleId = (req["sid"] as? String) ?? ""
        let title = (req["titl"] as? String) ?? ""
        let subtitle = (req["subt"] as? String) ?? ""
        let body = (req["body"] as? String) ?? ""
        return NotificationRecord(
            recId: recId, deliveredDate: deliveredDate,
            appBundleId: bundleId, title: title, subtitle: subtitle, body: body
        )
    }
}
