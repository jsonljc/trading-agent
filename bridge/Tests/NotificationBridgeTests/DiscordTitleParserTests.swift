import XCTest
@testable import NotificationBridgeCore

final class DiscordTitleParserTests: XCTestCase {
    func test_realDiscordTitleShape() {
        let r = DiscordTitleParser.parse("\u{2068}0xcow\u{2069} (\u{2068}#🏦丨chat\u{2069}, \u{2068}COMMUNITY CHAT\u{2069})")
        XCTAssertEqual(r?.author, "0xcow")
        XCTAssertEqual(r?.channel, "chat")
    }

    func test_plainAsciiChannel() {
        let r = DiscordTitleParser.parse("UndefinedMystic (#mystic, Stock Talk Insiders)")
        XCTAssertEqual(r?.author, "UndefinedMystic")
        XCTAssertEqual(r?.channel, "mystic")
    }

    func test_hyphenatedChannel() {
        let r = DiscordTitleParser.parse("user (#📈丨wall-st-engine, Server)")
        XCTAssertEqual(r?.author, "user")
        XCTAssertEqual(r?.channel, "wall-st-engine")
    }

    func test_titleWithoutChannelReturnsNil() {
        XCTAssertNil(DiscordTitleParser.parse("Some random title"))
    }

    func test_emptyReturnsNil() {
        XCTAssertNil(DiscordTitleParser.parse(""))
    }
}
