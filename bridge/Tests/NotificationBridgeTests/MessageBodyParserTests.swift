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
