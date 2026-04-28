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
