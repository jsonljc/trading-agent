import Foundation
import SQLite3

struct NotificationRecord {
    let recId: Int64
    let deliveredDate: Double
    let appBundleId: String
    let title: String
    let subtitle: String
    let body: String
}

class NotificationPoller {
    private let dbPath: String
    private var lastSeenId: Int64 = 0

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        dbPath = "\(home)/Library/Application Support/com.apple.notificationcenter/db2/db"
    }

    func pollNew() -> [NotificationRecord] {
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
                lastSeenId = recId
            }
        }
        return results
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
            recId: recId,
            deliveredDate: deliveredDate,
            appBundleId: bundleId,
            title: title,
            subtitle: subtitle,
            body: body
        )
    }
}
