import XCTest
import SQLite3
@testable import NotificationBridgeCore

final class NotificationDBPollerTests: XCTestCase {
    var tempDir: URL!
    var dbPath: String!

    override func setUp() {
        super.setUp()
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("notif-poller-test-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        dbPath = tempDir.appendingPathComponent("db").path
        createSchema()
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    func test_pollNew_returnsDecodedDiscordRow() {
        let recId = insertDiscordRow(
            bundleId: "com.hnc.Discord",
            title: "Stock Talk Insiders",
            subtitle: "#mystic",
            body: "UndefinedMystic: Long $SPY swing"
        )
        let poller = NotificationDBPoller(dbPath: dbPath, watchedChannels: ["mystic"], emitter: nil, dedup: FingerprintDedup(), startingRecId: recId - 1)
        let records = poller.pollNew()
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records[0].appBundleId, "com.hnc.Discord")
        XCTAssertEqual(records[0].subtitle, "#mystic")
    }

    func test_processRecord_emitsParsedSignal() {
        let captured = CapturingEmitter()
        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: captured,
            dedup: FingerprintDedup(),
            startingRecId: 0
        )
        let rec = NotificationRecord(
            recId: 1, deliveredDate: 0,
            appBundleId: "com.hnc.Discord",
            title: "Stock Talk Insiders",
            subtitle: "#mystic",
            body: "Author: msg body"
        )
        poller.process(rec)
        XCTAssertEqual(captured.events.count, 1)
        XCTAssertEqual(captured.events[0]["channel"], "mystic")
        XCTAssertEqual(captured.events[0]["author"], "Author")
        XCTAssertEqual(captured.events[0]["trigger_preview"], "msg body")
        XCTAssertEqual(captured.events[0]["source"], "notif_db")
    }

    func test_processRecord_dropsUnwatchedChannel() {
        let captured = CapturingEmitter()
        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: captured,
            dedup: FingerprintDedup(),
            startingRecId: 0
        )
        let rec = NotificationRecord(
            recId: 1, deliveredDate: 0,
            appBundleId: "com.hnc.Discord",
            title: "Stock Talk Insiders",
            subtitle: "#friends",
            body: "Author: not a watched channel"
        )
        poller.process(rec)
        XCTAssertTrue(captured.events.isEmpty)
    }

    func test_processRecord_dropsNonDiscord() {
        let captured = CapturingEmitter()
        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: captured,
            dedup: FingerprintDedup(),
            startingRecId: 0
        )
        let rec = NotificationRecord(
            recId: 1, deliveredDate: 0,
            appBundleId: "com.apple.Mail",
            title: "Inbox",
            subtitle: "#mystic",
            body: "Author: same channel name but different app"
        )
        poller.process(rec)
        XCTAssertTrue(captured.events.isEmpty)
    }

    func test_processRecord_dedupsRepeats() {
        let captured = CapturingEmitter()
        let dedup = FingerprintDedup()
        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: captured,
            dedup: dedup,
            startingRecId: 0
        )
        let rec = NotificationRecord(
            recId: 1, deliveredDate: 0,
            appBundleId: "com.hnc.Discord",
            title: "Stock Talk Insiders",
            subtitle: "#mystic",
            body: "Author: msg"
        )
        poller.process(rec)
        poller.process(rec)
        XCTAssertEqual(captured.events.count, 1)
    }

    // MARK: - Helpers

    private func createSchema() {
        var db: OpaquePointer?
        sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil)
        defer { sqlite3_close(db) }
        let sql = """
        CREATE TABLE record (
            rec_id INTEGER PRIMARY KEY AUTOINCREMENT,
            data BLOB,
            delivered_date REAL
        );
        """
        sqlite3_exec(db, sql, nil, nil, nil)
    }

    private func insertDiscordRow(bundleId: String, title: String, subtitle: String, body: String) -> Int64 {
        var db: OpaquePointer?
        sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READWRITE, nil)
        defer { sqlite3_close(db) }

        let req: NSDictionary = ["sid": bundleId, "titl": title, "subt": subtitle, "body": body]
        let outer: NSDictionary = ["req": req]
        let blob = try! NSKeyedArchiver.archivedData(withRootObject: outer, requiringSecureCoding: false)

        var stmt: OpaquePointer?
        sqlite3_prepare_v2(db, "INSERT INTO record (data, delivered_date) VALUES (?, ?)", -1, &stmt, nil)
        defer { sqlite3_finalize(stmt) }
        blob.withUnsafeBytes { buf in
            sqlite3_bind_blob(stmt, 1, buf.baseAddress, Int32(buf.count), nil)
        }
        sqlite3_bind_double(stmt, 2, 0)
        sqlite3_step(stmt)
        return sqlite3_last_insert_rowid(db)
    }
}

/// In-memory emitter for tests.
final class CapturingEmitter: SignalEmitter {
    var events: [[String: String]] = []
    func emit(_ event: [String: String]) {
        events.append(event)
    }
}
