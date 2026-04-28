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

    func test_pollNew_advancesLastSeenIdAcrossCalls() {
        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: nil,
            dedup: FingerprintDedup(),
            startingRecId: 0
        )
        let rec1 = insertDiscordRow(bundleId: "com.hnc.Discord", title: "S", subtitle: "#mystic", body: "A: msg1")
        XCTAssertEqual(poller.pollNew().count, 1)
        XCTAssertEqual(poller.pollNew().count, 0, "second call should pick up nothing new")
        let rec2 = insertDiscordRow(bundleId: "com.hnc.Discord", title: "S", subtitle: "#mystic", body: "B: msg2")
        XCTAssertGreaterThan(rec2, rec1)
        let next = poller.pollNew()
        XCTAssertEqual(next.count, 1)
        XCTAssertEqual(next[0].body, "B: msg2")
    }

    func test_pollNew_doesNotAdvanceLastSeenIdPastUndecodedRow() {
        // Insert a row with a malformed blob. Poll. Then rewrite the row's
        // blob with a valid plist and poll again. With the bug (lastSeenId
        // advances unconditionally) the second poll's SQL filter `rec_id > N`
        // skips the now-decodable row, dropping a trading signal forever.
        // With the fix, lastSeenId only advances on successful decode, so
        // the second poll re-reads the row and returns it.
        let badId = insertMalformedRow()

        let poller = NotificationDBPoller(
            dbPath: dbPath,
            watchedChannels: ["mystic"],
            emitter: nil,
            dedup: FingerprintDedup(),
            startingRecId: badId - 1
        )
        XCTAssertEqual(poller.pollNew().count, 0, "malformed row must not be returned as a decoded record")

        // Rewrite the same rec_id with a valid Discord plist.
        rewriteRowAsValidDiscord(recId: badId, subtitle: "#mystic", body: "Author: now-readable")

        let second = poller.pollNew()
        XCTAssertEqual(second.count, 1, "row must be re-readable on next poll if it later becomes valid; lastSeenId must not have advanced past an undecodable row")
        XCTAssertEqual(second[0].body, "Author: now-readable")
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
            title: "Author (#mystic, Stock Talk Insiders)",
            subtitle: "",
            body: "msg body"
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
            title: "Author (#friends, Stock Talk Insiders)",
            subtitle: "",
            body: "not a watched channel"
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
            title: "Author (#mystic, server)",
            subtitle: "",
            body: "same channel name but different app"
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
            title: "Author (#mystic, server)",
            subtitle: "",
            body: "msg"
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

        // Match the real macOS notification DB shape: bundle id at OUTER level
        // under `app`; title / body in inner `req` dict. Stored as a plain
        // binary plist (NOT NSKeyedArchiver), matching the on-disk format.
        let req: NSDictionary = ["titl": title, "subt": subtitle, "body": body]
        let outer: NSDictionary = ["app": bundleId, "req": req]
        let blob = try! PropertyListSerialization.data(fromPropertyList: outer, format: .binary, options: 0)

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

    /// Replace an existing row's blob with a valid Discord plist.
    private func rewriteRowAsValidDiscord(recId: Int64, subtitle: String, body: String) {
        var db: OpaquePointer?
        sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READWRITE, nil)
        defer { sqlite3_close(db) }
        let req: NSDictionary = ["titl": "S", "subt": subtitle, "body": body]
        let outer: NSDictionary = ["app": "com.hnc.Discord", "req": req]
        let blob = try! PropertyListSerialization.data(fromPropertyList: outer, format: .binary, options: 0)
        var stmt: OpaquePointer?
        sqlite3_prepare_v2(db, "UPDATE record SET data = ? WHERE rec_id = ?", -1, &stmt, nil)
        defer { sqlite3_finalize(stmt) }
        blob.withUnsafeBytes { buf in
            sqlite3_bind_blob(stmt, 1, buf.baseAddress, Int32(buf.count), nil)
        }
        sqlite3_bind_int64(stmt, 2, recId)
        sqlite3_step(stmt)
    }

    /// Insert a row whose blob is not a valid plist — simulates a
    /// schema variant or partially-written row.
    private func insertMalformedRow() -> Int64 {
        var db: OpaquePointer?
        sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READWRITE, nil)
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        sqlite3_prepare_v2(db, "INSERT INTO record (data, delivered_date) VALUES (?, ?)", -1, &stmt, nil)
        defer { sqlite3_finalize(stmt) }
        let blob = Data([0xDE, 0xAD, 0xBE, 0xEF])
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
