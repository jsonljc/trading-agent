import Foundation

public enum ChannelNameParser {
    public static func parse(_ raw: String) -> String? {
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
