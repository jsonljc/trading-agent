import Foundation

public enum AXNoiseFilter {
    public static func isNoise(channel: String, author: String, body: String) -> Bool {
        let trimmed = body.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasSuffix("- Discord") { return true }
        if trimmed.hasPrefix("#") && trimmed.count < 80 { return true }
        if author.isEmpty && trimmed.count < 30 { return true }
        return false
    }
}
