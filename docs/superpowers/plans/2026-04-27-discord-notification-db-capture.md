# Discord Notification DB Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unreliable AX banner-clicker path in the Discord bridge with a direct read of the macOS notification SQLite database, keeping the AX watcher as a fallback for the active-channel case.

**Architecture:** Swift bridge gets a new `NotificationDBPoller` that opens `~/Library/Application Support/com.apple.notificationcenter/db2/db` read-only, polls every 500 ms for new Discord rows, parses `subtitle`/`body` into `(channel, author, message)`, deduplicates against the existing fingerprint ring buffer, and emits to the same Unix socket the AX watcher uses today. The `NotificationBannerClicker` (Swift) and `NotificationBannerPoller` (Python) — both unreliable on macOS 26 — are removed.

**Tech Stack:** Swift 6.3 (existing bridge), SQLite3 (already imported in `NotificationPoller.swift`), `NSKeyedUnarchiver` for record decoding, XCTest (newly added), Python 3.14 (only for the agent-side cleanup).

**Spec:** `docs/superpowers/specs/2026-04-27-discord-notification-db-capture-design.md`

---

## File Structure

**New files:**
- `bridge/Sources/NotificationBridge/ChannelNameParser.swift` — pure function: notification subtitle → watched channel string
- `bridge/Sources/NotificationBridge/MessageBodyParser.swift` — pure function: notification body → `(author, message)`
- `bridge/Sources/NotificationBridge/FingerprintDedup.swift` — shared ring-buffer dedup (extracted from `AXDiscordWatcher.swift`)
- `bridge/Tests/NotificationBridgeTests/ChannelNameParserTests.swift`
- `bridge/Tests/NotificationBridgeTests/MessageBodyParserTests.swift`
- `bridge/Tests/NotificationBridgeTests/FingerprintDedupTests.swift`
- `bridge/Tests/NotificationBridgeTests/NotificationDBPollerTests.swift`

**Modified files:**
- `bridge/Package.swift` — add test target, expose Sources as a library target so tests can import
- `bridge/Sources/NotificationBridge/NotificationPoller.swift` — extend with polling timer, watched-channels filter, emit; rename internal type `NotificationPoller` → `NotificationDBPoller`; keep public file path the same
- `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift` — remove inline fingerprint dedup (use shared `FingerprintDedup`); tighten message extractor to drop window titles and channel headers
- `bridge/Sources/NotificationBridge/main.swift` — instantiate `NotificationDBPoller`, drop `NotificationBannerClicker`
- `main.py` — drop import + `start()` of `NotificationBannerPoller`
- `bin/agent-listen-only` — drop Python banner poller import + start
- `bin/agent-status` — add Full Disk Access reminder
- `infra/bridge_client/notification_poller.py` — DELETE

**Deleted files:**
- `bridge/Sources/NotificationBridge/NotificationBannerClicker.swift`
- `infra/bridge_client/notification_poller.py`

---

## Task 1: Add Swift test target

**Files:**
- Modify: `bridge/Package.swift`
- Create: `bridge/Tests/NotificationBridgeTests/SmokeTest.swift`

The bridge has no test target today. Before writing parser tests we have to create one. The trick: an `executableTarget` cannot be imported by a test target, so we restructure into a library target + thin executable.

- [ ] **Step 1: Replace `bridge/Package.swift` with split-target version**

```swift
// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NotificationBridge",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "NotificationBridgeCore",
            path: "Sources/NotificationBridge",
            exclude: ["main.swift"]
        ),
        .executableTarget(
            name: "NotificationBridge",
            dependencies: ["NotificationBridgeCore"],
            path: "Sources/NotificationBridge",
            sources: ["main.swift"]
        ),
        .testTarget(
            name: "NotificationBridgeTests",
            dependencies: ["NotificationBridgeCore"],
            path: "Tests/NotificationBridgeTests"
        ),
    ]
)
```

- [ ] **Step 2: Create `bridge/Tests/NotificationBridgeTests/SmokeTest.swift`**

```swift
import XCTest
@testable import NotificationBridgeCore

final class SmokeTest: XCTestCase {
    func test_packageBuildsAndTestRuns() {
        XCTAssertEqual(1 + 1, 2)
    }
}
```

- [ ] **Step 3: Verify all classes compile under the new structure**

Existing files (`AXDiscordWatcher.swift`, `NotificationPoller.swift`, `NotificationBannerClicker.swift`, `SocketEmitter.swift`) need to be importable from tests. They use `internal` access (default) which works once they're in a library target. No code change required.

Add `import NotificationBridgeCore` line at top of `bridge/Sources/NotificationBridge/main.swift`:

```swift
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
```

- [ ] **Step 4: Build and run tests**

Run from `bridge/`:
```bash
cd bridge && swift test
```

Expected: build succeeds, `SmokeTest.test_packageBuildsAndTestRuns` passes.

- [ ] **Step 5: Commit**

```bash
git add bridge/Package.swift bridge/Tests bridge/Sources/NotificationBridge/main.swift
git commit -m "test(bridge): split into library + executable, add XCTest target"
```

---

## Task 2: ChannelNameParser

**Files:**
- Create: `bridge/Sources/NotificationBridge/ChannelNameParser.swift`
- Create: `bridge/Tests/NotificationBridgeTests/ChannelNameParserTests.swift`

Parses Discord notification `subtitle` (e.g. `#🏦丨chat`) into the canonical lowercase channel string used by `watchedChannels`.

Discord notifications come in three observed formats:
- `#mystic` (plain)
- `#🏦丨chat` (emoji + ideographic separator `丨` U+4E28)
- `#📈丨wall-st-engine` (emoji + separator + hyphenated)

If parsing fails, return `nil` and the caller drops the notification.

- [ ] **Step 1: Write failing tests at `bridge/Tests/NotificationBridgeTests/ChannelNameParserTests.swift`**

```swift
import XCTest
@testable import NotificationBridgeCore

final class ChannelNameParserTests: XCTestCase {
    func test_plainChannel() {
        XCTAssertEqual(ChannelNameParser.parse("#mystic"), "mystic")
    }

    func test_emojiPrefix() {
        XCTAssertEqual(ChannelNameParser.parse("#🏦丨chat"), "chat")
    }

    func test_emojiPrefixWithHyphens() {
        XCTAssertEqual(ChannelNameParser.parse("#📈丨wall-st-engine"), "wall-st-engine")
    }

    func test_alreadyLowercase() {
        XCTAssertEqual(ChannelNameParser.parse("#alerts"), "alerts")
    }

    func test_uppercaseGetsLowercased() {
        XCTAssertEqual(ChannelNameParser.parse("#ALERTS"), "alerts")
    }

    func test_emptyReturnsNil() {
        XCTAssertNil(ChannelNameParser.parse(""))
    }

    func test_noHashReturnsNil() {
        XCTAssertNil(ChannelNameParser.parse("mystic"))
    }
}
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd bridge && swift test --filter ChannelNameParserTests
```

Expected: compile error — `ChannelNameParser` not defined.

- [ ] **Step 3: Implement `ChannelNameParser.swift`**

```swift
import Foundation

enum ChannelNameParser {
    static func parse(_ raw: String) -> String? {
        guard raw.hasPrefix("#") else { return nil }
        var s = String(raw.dropFirst())
        // Strip leading non-ASCII chars (emojis, ideographic separators) until we hit
        // an ASCII letter, digit, or hyphen — those are the start of the channel name.
        while let first = s.unicodeScalars.first {
            if first.isASCII && (CharacterSet.alphanumerics.contains(first) || first == "-") {
                break
            }
            s = String(s.dropFirst())
        }
        guard !s.isEmpty else { return nil }
        return s.lowercased()
    }
}
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
cd bridge && swift test --filter ChannelNameParserTests
```

Expected: all 7 pass.

- [ ] **Step 5: Commit**

```bash
git add bridge/Sources/NotificationBridge/ChannelNameParser.swift bridge/Tests/NotificationBridgeTests/ChannelNameParserTests.swift
git commit -m "feat(bridge): ChannelNameParser strips emoji+separator prefix"
```

---

## Task 3: MessageBodyParser

**Files:**
- Create: `bridge/Sources/NotificationBridge/MessageBodyParser.swift`
- Create: `bridge/Tests/NotificationBridgeTests/MessageBodyParserTests.swift`

Splits the notification `body` field into `(author, message)`. Most Discord server notifications come as `"<author>: <message text>"`. DM and system messages may not have the `: ` separator; in that case author is empty and the whole body is the message.

- [ ] **Step 1: Write failing tests**

```swift
import XCTest
@testable import NotificationBridgeCore

final class MessageBodyParserTests: XCTestCase {
    func test_authorAndMessage() {
        let r = MessageBodyParser.parse("UndefinedMystic: Long $SPY swing")
        XCTAssertEqual(r.author, "UndefinedMystic")
        XCTAssertEqual(r.message, "Long $SPY swing")
    }

    func test_messageContainsColon() {
        let r = MessageBodyParser.parse("Trader: target: 500 by EOD")
        XCTAssertEqual(r.author, "Trader")
        XCTAssertEqual(r.message, "target: 500 by EOD")
    }

    func test_noColonReturnsEmptyAuthor() {
        let r = MessageBodyParser.parse("system message text here")
        XCTAssertEqual(r.author, "")
        XCTAssertEqual(r.message, "system message text here")
    }

    func test_emptyBody() {
        let r = MessageBodyParser.parse("")
        XCTAssertEqual(r.author, "")
        XCTAssertEqual(r.message, "")
    }

    func test_authorTrimmed() {
        let r = MessageBodyParser.parse("  Jason  : message")
        XCTAssertEqual(r.author, "Jason")
        XCTAssertEqual(r.message, "message")
    }
}
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd bridge && swift test --filter MessageBodyParserTests
```

Expected: compile error.

- [ ] **Step 3: Implement `MessageBodyParser.swift`**

```swift
import Foundation

enum MessageBodyParser {
    struct Result {
        let author: String
        let message: String
    }

    static func parse(_ raw: String) -> Result {
        guard let range = raw.range(of: ": ") else {
            return Result(author: "", message: raw)
        }
        let author = raw[..<range.lowerBound].trimmingCharacters(in: .whitespaces)
        let message = String(raw[range.upperBound...])
        return Result(author: author, message: message)
    }
}
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
cd bridge && swift test --filter MessageBodyParserTests
```

Expected: all 5 pass.

- [ ] **Step 5: Commit**

```bash
git add bridge/Sources/NotificationBridge/MessageBodyParser.swift bridge/Tests/NotificationBridgeTests/MessageBodyParserTests.swift
git commit -m "feat(bridge): MessageBodyParser splits 'author: text' notification body"
```

---

## Task 4: Extract FingerprintDedup

**Files:**
- Create: `bridge/Sources/NotificationBridge/FingerprintDedup.swift`
- Create: `bridge/Tests/NotificationBridgeTests/FingerprintDedupTests.swift`
- Modify: `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift` (lines that hold `seenFingerprints`, `fingerprintQueue`, `markSeen`, `fingerprint(...)`)

Pull the ring-buffer fingerprint dedup out of `AXDiscordWatcher` into a standalone class so both pollers can share it. Same algorithm, same 500-entry cap. Thread-safe via internal serial queue.

- [ ] **Step 1: Write failing tests at `FingerprintDedupTests.swift`**

```swift
import XCTest
@testable import NotificationBridgeCore

final class FingerprintDedupTests: XCTestCase {
    func test_firstSeenReturnsTrue() {
        let d = FingerprintDedup(maxEntries: 10)
        XCTAssertTrue(d.markSeen("abc"))
    }

    func test_repeatedFingerprintReturnsFalse() {
        let d = FingerprintDedup(maxEntries: 10)
        _ = d.markSeen("abc")
        XCTAssertFalse(d.markSeen("abc"))
    }

    func test_distinctFingerprintsAllReturnTrue() {
        let d = FingerprintDedup(maxEntries: 10)
        XCTAssertTrue(d.markSeen("a"))
        XCTAssertTrue(d.markSeen("b"))
        XCTAssertTrue(d.markSeen("c"))
    }

    func test_evictionLetsOldFingerprintReappear() {
        let d = FingerprintDedup(maxEntries: 2)
        _ = d.markSeen("a")
        _ = d.markSeen("b")
        _ = d.markSeen("c")  // evicts "a"
        XCTAssertTrue(d.markSeen("a"))
    }

    func test_makeFingerprint_combinesFields() {
        let f1 = FingerprintDedup.make(channel: "mystic", author: "U", body: "Long")
        let f2 = FingerprintDedup.make(channel: "mystic", author: "U", body: "Long")
        let f3 = FingerprintDedup.make(channel: "alerts", author: "U", body: "Long")
        XCTAssertEqual(f1, f2)
        XCTAssertNotEqual(f1, f3)
    }
}
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd bridge && swift test --filter FingerprintDedupTests
```

Expected: compile error.

- [ ] **Step 3: Implement `FingerprintDedup.swift`**

```swift
import Foundation

/// Thread-safe ring-buffer dedup keyed on a string fingerprint.
final class FingerprintDedup {
    private var seen: [String] = []
    private let maxEntries: Int
    private let queue = DispatchQueue(label: "fingerprint.dedup.serial")

    init(maxEntries: Int = 500) {
        self.maxEntries = maxEntries
    }

    /// Returns true if this is the first time we've seen `fp`, false if duplicate.
    func markSeen(_ fp: String) -> Bool {
        queue.sync {
            if seen.contains(fp) { return false }
            seen.append(fp)
            if seen.count > maxEntries { seen.removeFirst() }
            return true
        }
    }

    /// Stable fingerprint over the fields that identify a unique signal.
    static func make(channel: String, author: String, body: String) -> String {
        "\(channel)|\(author)|\(body)"
    }
}
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
cd bridge && swift test --filter FingerprintDedupTests
```

Expected: all 5 pass.

- [ ] **Step 5: Update `AXDiscordWatcher.swift` to use the shared class**

Open `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift`. Find and remove the inline fingerprint state:

```swift
// REMOVE these instance properties:
private var seenFingerprints: [String] = []
private let maxSeen = 500
private let fingerprintQueue = DispatchQueue(label: "ax.fingerprint.serial")

// REMOVE the private helper methods:
private func fingerprint(channel: String, author: String, body: String) -> String { ... }
private func markSeen(_ fp: String) -> Bool { ... }
```

Add a `dedup` property in the class:

```swift
private let dedup = FingerprintDedup()
```

Replace the call sites:
- Line ~129 (`let fp = fingerprint(channel: msg.channel, ...); guard markSeen(fp) else { return }`) becomes:
  ```swift
  let fp = FingerprintDedup.make(channel: msg.channel, author: msg.author, body: msg.body)
  guard dedup.markSeen(fp) else { return }
  ```
- Line ~151 (in `reconcile`): same substitution.

- [ ] **Step 6: Run all tests, confirm AX tests still pass**

```bash
cd bridge && swift test
```

Expected: all pass (smoke + ChannelNameParser + MessageBodyParser + FingerprintDedup). AXDiscordWatcher has no tests yet, so it just has to compile.

- [ ] **Step 7: Commit**

```bash
git add bridge/Sources/NotificationBridge/FingerprintDedup.swift bridge/Tests/NotificationBridgeTests/FingerprintDedupTests.swift bridge/Sources/NotificationBridge/AXDiscordWatcher.swift
git commit -m "refactor(bridge): extract FingerprintDedup into shared component"
```

---

## Task 5: NotificationDBPoller

**Files:**
- Modify: `bridge/Sources/NotificationBridge/NotificationPoller.swift` (rename internal class, add poll loop, add emit)
- Create: `bridge/Tests/NotificationBridgeTests/NotificationDBPollerTests.swift`

The existing `NotificationPoller` class only has `pollNew()` returning records. We extend it into a full `NotificationDBPoller` that:
1. Initializes `lastSeenId` to the current max `rec_id` (no historical replay).
2. Schedules a 500ms timer that calls `pollNew()`.
3. For each Discord record (`appBundleId == "com.hnc.Discord"`), parses channel + body, dedups, emits.
4. Logs to stdout for observability.

- [ ] **Step 1: Write failing tests**

```swift
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
        XCTAssertEqual(captured.events[0]["body"], "msg body")
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

/// In-memory emitter for tests. Captures the dictionary that would have been sent.
final class CapturingEmitter: SignalEmitter {
    var events: [[String: String]] = []
    func emit(_ event: [String: String]) {
        events.append(event)
    }
}
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd bridge && swift test --filter NotificationDBPollerTests
```

Expected: compile errors — `NotificationDBPoller`, `SignalEmitter`, expanded `init`, `process` not defined.

- [ ] **Step 3: Define `SignalEmitter` protocol and adapt `SocketEmitter`**

Open `bridge/Sources/NotificationBridge/SocketEmitter.swift`. At the top of the file, **above** the existing `class SocketEmitter` declaration, add the protocol:

```swift
import Foundation
import Darwin

/// Anything that can emit a signal as a flat string-string dictionary
/// over the bridge's transport (Unix socket in production, captured in memory for tests).
protocol SignalEmitter {
    func emit(_ event: [String: String])
}

class SocketEmitter: SignalEmitter {
    // ... existing body unchanged
```

Make sure `class SocketEmitter` declares conformance: `class SocketEmitter: SignalEmitter`. Existing `func emit(_ event: [String: String])` already matches the protocol — no change to method body.

- [ ] **Step 4: Replace the contents of `bridge/Sources/NotificationBridge/NotificationPoller.swift`**

```swift
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

final class NotificationDBPoller {
    private let dbPath: String
    private let watchedChannels: Set<String>
    private let emitter: SignalEmitter?
    private let dedup: FingerprintDedup
    private var lastSeenId: Int64
    private var timer: Timer?

    init(dbPath: String? = nil,
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

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.tick()
        }
        print("NotificationDBPoller running. db=\(dbPath) channels=\(watchedChannels.sorted()) startingRecId=\(lastSeenId)")
    }

    func tick() {
        for record in pollNew() {
            process(record)
        }
    }

    /// Reads new rows from the notification DB. Public for testability.
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
            }
            lastSeenId = max(lastSeenId, recId)
        }
        return results
    }

    /// Filter, parse, dedup, emit. Public for testability.
    func process(_ record: NotificationRecord) {
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
```

Note: `deliveredDate + 978307200` converts macOS Core Foundation absolute time (epoch 2001-01-01) to Unix epoch.

- [ ] **Step 5: Run tests, confirm pass**

```bash
cd bridge && swift test --filter NotificationDBPollerTests
```

Expected: all 5 pass.

- [ ] **Step 6: Commit**

```bash
git add bridge/Sources/NotificationBridge/NotificationPoller.swift bridge/Sources/NotificationBridge/SocketEmitter.swift bridge/Tests/NotificationBridgeTests/NotificationDBPollerTests.swift
git commit -m "feat(bridge): NotificationDBPoller reads notif DB, emits to socket"
```

---

## Task 6: Tighten AXDiscordWatcher message extractor

**Files:**
- Modify: `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift`
- Create: `bridge/Tests/NotificationBridgeTests/AXNoiseFilterTests.swift`

The AX watcher currently emits when `extractMessage` returns any non-nil result. We add a noise filter that drops:
- Window titles matching `* | Stock Talk Insiders - Discord` or `* - Discord`
- Channel-header text starting with `#` followed by emoji
- Empty-author entries with body shorter than 30 chars

Pull the noise check into a static helper for testability.

- [ ] **Step 1: Write failing tests at `AXNoiseFilterTests.swift`**

```swift
import XCTest
@testable import NotificationBridgeCore

final class AXNoiseFilterTests: XCTestCase {
    func test_realMessagePasses() {
        XCTAssertFalse(AXNoiseFilter.isNoise(
            channel: "mystic", author: "User", body: "Long $SPY high conviction earnings play"
        ))
    }

    func test_windowTitleIsNoise() {
        XCTAssertTrue(AXNoiseFilter.isNoise(
            channel: "chat", author: "", body: "#🏦丨chat | Stock Talk Insiders - Discord"
        ))
    }

    func test_anyDiscordTitleIsNoise() {
        XCTAssertTrue(AXNoiseFilter.isNoise(
            channel: "alerts", author: "", body: "Friends - Discord"
        ))
    }

    func test_channelHeaderIsNoise() {
        XCTAssertTrue(AXNoiseFilter.isNoise(
            channel: "mystic", author: "", body: "#📈丨mystic"
        ))
    }

    func test_shortAnonBodyIsNoise() {
        XCTAssertTrue(AXNoiseFilter.isNoise(
            channel: "mystic", author: "", body: "ok"
        ))
    }

    func test_shortMessageWithAuthorPasses() {
        XCTAssertFalse(AXNoiseFilter.isNoise(
            channel: "mystic", author: "User", body: "long"
        ))
    }
}
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd bridge && swift test --filter AXNoiseFilterTests
```

Expected: compile error.

- [ ] **Step 3: Create `bridge/Sources/NotificationBridge/AXNoiseFilter.swift`**

```swift
import Foundation

enum AXNoiseFilter {
    static func isNoise(channel: String, author: String, body: String) -> Bool {
        let trimmed = body.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasSuffix("- Discord") { return true }
        if trimmed.hasPrefix("#") && trimmed.count < 80 { return true }
        if author.isEmpty && trimmed.count < 30 { return true }
        return false
    }
}
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
cd bridge && swift test --filter AXNoiseFilterTests
```

Expected: all 6 pass.

- [ ] **Step 5: Wire `AXNoiseFilter` into `AXDiscordWatcher.swift`**

In `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift`, find the `handleAXNotification` method (around line 120) and insert the filter check after `extractMessage` succeeds and before fingerprint:

```swift
guard let msg = extractMessage(from: element) else { return }
guard watchedChannels.contains(msg.channel) else { return }
guard !AXNoiseFilter.isNoise(channel: msg.channel, author: msg.author, body: msg.body) else { return }
let fp = FingerprintDedup.make(channel: msg.channel, author: msg.author, body: msg.body)
guard dedup.markSeen(fp) else { return }
emit(channel: msg.channel, author: msg.author, body: msg.body, source: "ax_event")
```

Apply the same filter inside `reconcile(...)`:

```swift
guard self.watchedChannels.contains(ch) else { return }
guard !AXNoiseFilter.isNoise(channel: ch, author: "reconcile", body: body) else { return }
let fp = FingerprintDedup.make(channel: ch, author: "reconcile", body: body)
guard self.dedup.markSeen(fp) else { return }
self.emit(channel: ch, author: "reconcile", body: body, source: "reconciliation")
```

- [ ] **Step 6: Run all tests, confirm pass**

```bash
cd bridge && swift test
```

Expected: all parser + dedup + filter tests pass; AX file compiles.

- [ ] **Step 7: Commit**

```bash
git add bridge/Sources/NotificationBridge/AXNoiseFilter.swift bridge/Sources/NotificationBridge/AXDiscordWatcher.swift bridge/Tests/NotificationBridgeTests/AXNoiseFilterTests.swift
git commit -m "feat(bridge): AXNoiseFilter drops window titles + channel headers from AX events"
```

---

## Task 7: Wire main.swift, delete NotificationBannerClicker

**Files:**
- Modify: `bridge/Sources/NotificationBridge/main.swift`
- Delete: `bridge/Sources/NotificationBridge/NotificationBannerClicker.swift`

Replace the dead-code wiring with the new `NotificationDBPoller`.

- [ ] **Step 1: Replace `bridge/Sources/NotificationBridge/main.swift`**

```swift
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
```

- [ ] **Step 2: Update `AXDiscordWatcher.swift` initializer to accept shared `dedup`**

Current signature:
```swift
init(bundleId: String, watchedChannels: [String], socketPath: String, logPath: String) {
    ...
    self.emitter = SocketEmitter(socketPath: socketPath)
    ...
}
```

New signature:
```swift
init(bundleId: String, watchedChannels: [String], socketPath: String, logPath: String, dedup: FingerprintDedup) {
    self.bundleId = bundleId
    self.watchedChannels = Set(watchedChannels)
    self.emitter = SocketEmitter(socketPath: socketPath)
    self.logPath = logPath
    self.dedup = dedup
}
```

- [ ] **Step 3: Delete `bridge/Sources/NotificationBridge/NotificationBannerClicker.swift`**

```bash
git rm bridge/Sources/NotificationBridge/NotificationBannerClicker.swift
```

- [ ] **Step 4: Build the bridge**

```bash
cd bridge && swift build
```

Expected: `Build complete!`. No references to `NotificationBannerClicker` remain.

- [ ] **Step 5: Run all tests**

```bash
cd bridge && swift test
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bridge/Sources/NotificationBridge/main.swift bridge/Sources/NotificationBridge/AXDiscordWatcher.swift
git commit -m "feat(bridge): wire NotificationDBPoller in main; remove NotificationBannerClicker"
```

---

## Task 8: Remove Python NotificationBannerPoller

**Files:**
- Delete: `infra/bridge_client/notification_poller.py`
- Modify: `main.py`
- Modify: `bin/agent-listen-only`

Both files import `NotificationBannerPoller` and start it. With the Swift DB poller doing the job, the Python click-poller is dead weight.

- [ ] **Step 1: Remove the import + start from `main.py`**

Open `main.py`. Remove the import line (around line 27):
```python
from infra.bridge_client.notification_poller import NotificationBannerPoller
```

Remove the `start()` call (around line 110). The block currently reads:
```python
    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 2b ready. Listening on %s", socket_path)
    try:
        NotificationBannerPoller().start()
        reconciler.start()
        await reader.start(handle_event)
```

Change to:
```python
    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 2b ready. Listening on %s", socket_path)
    try:
        reconciler.start()
        await reader.start(handle_event)
```

- [ ] **Step 2: Remove the import + start from `bin/agent-listen-only`**

Open `bin/agent-listen-only`. Remove the import:
```python
from infra.bridge_client.notification_poller import NotificationBannerPoller
```

Remove the start call inside `main`:
```python
    NotificationBannerPoller().start()
    print(f"NotificationBannerPoller started — will click Discord banners to navigate.", flush=True)
```

Replace the print with:
```python
    print("Bridge handles signal capture; this listener just prints what arrives.", flush=True)
```

- [ ] **Step 3: Delete the file**

```bash
git rm infra/bridge_client/notification_poller.py
```

- [ ] **Step 4: Run python unit tests**

```bash
.venv/bin/python -m pytest tests/unit -q
```

Expected: 115 pass, no import errors. (No test currently imports the removed module.)

- [ ] **Step 5: Verify `bin/agent-listen-only` runs**

```bash
.venv/bin/python bin/agent-listen-only --socket /tmp/trading_bridge_test.sock &
sleep 2
.venv/bin/python inject_event.py "ping" --channel mystic --socket /tmp/trading_bridge_test.sock
sleep 1
kill %1 2>/dev/null
rm -f /tmp/trading_bridge_test.sock
```

Expected: listener starts without ImportError, receives the ping event.

- [ ] **Step 6: Commit**

```bash
git add main.py bin/agent-listen-only
git commit -m "feat(agent): remove Python NotificationBannerPoller (replaced by Swift DB poller)"
```

---

## Task 9: Add Full Disk Access reminder to agent-status

**Files:**
- Modify: `bin/agent-status`

The new bridge requires Full Disk Access. Add a probe + warning to `agent-status`.

- [ ] **Step 1: Modify `bin/agent-status` to detect FDA availability**

Find the existing block at the end of `bin/agent-status` that prints the market data warning. Above it, insert:

```bash
# Probe for Full Disk Access by attempting a 1-byte read of the notification DB.
NOTIF_DB="$HOME/Library/Application Support/com.apple.notificationcenter/db2/db"
if [ ! -r "$NOTIF_DB" ]; then
    echo
    echo "  ⚠️  Full Disk Access NOT granted to the bridge."
    echo "     The new NotificationDBPoller requires reading $NOTIF_DB"
    echo "     Open System Settings → Privacy & Security → Full Disk Access"
    echo "     and enable NotificationBridge (or Terminal/iTerm if launching by hand)."
fi
```

- [ ] **Step 2: Run agent-status to verify**

```bash
bin/agent-status
```

Expected: if FDA not granted, the new warning prints; if granted, no warning.

- [ ] **Step 3: Commit**

```bash
git add bin/agent-status
git commit -m "feat(ops): agent-status warns when Full Disk Access not granted"
```

---

## Task 10: End-to-end manual smoke test

**Files:** none (verification only)

This task has no code changes — it's the manual verification gate. Document the outcomes in the commit message.

- [ ] **Step 1: Grant Full Disk Access**

System Settings → Privacy & Security → Full Disk Access → toggle ON for `bridge/.build/debug/NotificationBridge` (or for Terminal/iTerm if running interactively). May require restart of the bridge process.

- [ ] **Step 2: Verify watched channels are not muted in Discord**

In Discord, right-click each of: `mystic, alerts, trades, wall-st-engine, stock-talk-portfolio, chat, yonezu, pup-danny, urkel, gladiator, graddox, phat, grid`. Confirm each is set to "All Messages" (not "Only @mentions" or "Nothing"). Server-wide notification muting also needs to be off for these channels.

- [ ] **Step 3: Build the bridge cleanly**

```bash
cd bridge && swift build
```

Expected: `Build complete!`.

- [ ] **Step 4: Start the listener and the bridge**

In one terminal:
```bash
rm -f /tmp/trading_bridge.sock
.venv/bin/python bin/agent-listen-only
```

In another terminal:
```bash
bridge/.build/debug/NotificationBridge /tmp/trading_bridge.sock
```

The bridge should print `NotificationDBPoller running. db=...` on startup.

- [ ] **Step 5: Trigger a Discord notification**

While focused on a non-watched channel (e.g. `friends---discord`), have a real or test message land in a watched channel (e.g. `mystic`). Within ~500 ms the listener should print:

```
[HH:MM:SS] # N src=notif_db ch=mystic by=<author>
        <message body>
```

- [ ] **Step 6: Trigger an active-channel message (AX fallback)**

Click into a watched channel (e.g. `mystic`). Have a message arrive there. Within a couple of AX events the listener should print:

```
[HH:MM:SS] # N src=ax_event ch=mystic by=<author>
        <message body>
```

`source` should differ between the two cases.

- [ ] **Step 7: Verify dedup**

Send the same message twice in close succession. Only one event should print (one of the two dedups against the other).

- [ ] **Step 8: Commit verification record**

If everything works, write a brief manual-test record to `docs/superpowers/plans/2026-04-27-discord-notification-db-capture-verification.md` with:
- Date/time of test
- Whether `notif_db` path captured a non-active-channel message
- Whether `ax_event` path captured an active-channel message
- Whether dedup worked

```bash
git add docs/superpowers/plans/2026-04-27-discord-notification-db-capture-verification.md
git commit -m "docs: record end-to-end verification of NotificationDBPoller"
```

---

## Self-Review

**Spec coverage:**
- ✅ NotificationDBPoller: Task 5
- ✅ Channel name parser: Task 2
- ✅ Author parser: Task 3
- ✅ Hybrid AX fallback: AX preserved in Tasks 6, 7
- ✅ Tightened AX filter: Task 6
- ✅ FingerprintDedup shared: Task 4
- ✅ Drop NotificationBannerClicker: Task 7
- ✅ Drop Python NotificationBannerPoller: Task 8
- ✅ Permissions docs (FDA): Task 9
- ✅ Channel-mute caveat: Task 10 step 2
- ✅ End-to-end verification: Task 10
- ✅ `notif_db` source label: Task 5 step 4
- ✅ 500 ms poll: Task 5 step 4
- ✅ `lastSeenId` initialized to current max on startup: Task 5 step 4 (`currentMaxRecId`)

**Type consistency:**
- `SignalEmitter` protocol defined Task 5 step 3, used in Tasks 5 (poller), 7 (kept implicitly via SocketEmitter conforming).
- `FingerprintDedup` API consistent across Tasks 4, 5, 6, 7.
- `NotificationDBPoller.process(_:)` signature matches usage in tests and `tick()`.
- `AXDiscordWatcher` constructor adds `dedup:` parameter in Task 7 — consistent with the property added in Task 4.

**Placeholder scan:** none.

**Scope:** Single subsystem (Discord signal capture). Single implementation plan, ~10 tasks, 1 day of work.

