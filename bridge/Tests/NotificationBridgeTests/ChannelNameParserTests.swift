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
