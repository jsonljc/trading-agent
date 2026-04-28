import Foundation

/// Extracts (author, channel) from Discord's macOS notification title.
///
/// Real titles arrive bidi-isolated, e.g.:
///   "\u{2068}0xcow\u{2069} (\u{2068}#🏦丨chat\u{2069}, \u{2068}COMMUNITY CHAT\u{2069})"
///
/// We strip the bidi marks (U+2068 / U+2069), then look for the
/// `<author> (#<channel>, <server>)` shape. If we can't find a `(#…, ` segment,
/// the title is something we don't know how to parse and we return nil.
public enum DiscordTitleParser {
    public struct Result {
        public let author: String
        public let channel: String
    }

    public static func parse(_ raw: String) -> Result? {
        // Strip Unicode bidi-isolate marks Discord injects.
        var s = raw
        s.removeAll { $0 == "\u{2068}" || $0 == "\u{2069}" }

        // Need both " (#" (start of channel segment) and either ", " or ")"
        // to bound it.
        guard let openRange = s.range(of: " (#") else { return nil }
        let author = String(s[..<openRange.lowerBound]).trimmingCharacters(in: .whitespaces)

        // Channel runs from "#" + 1 char to the next "," or ")".
        let afterHash = s.index(openRange.upperBound, offsetBy: 0)  // position right after "#"
        let tail = s[afterHash...]
        let endChar = tail.firstIndex(where: { $0 == "," || $0 == ")" }) ?? tail.endIndex
        let rawChannel = "#" + String(tail[..<endChar])  // re-attach # for ChannelNameParser
        guard let channel = ChannelNameParser.parse(rawChannel) else { return nil }

        return Result(author: author, channel: channel)
    }
}
