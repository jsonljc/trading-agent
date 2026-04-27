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
