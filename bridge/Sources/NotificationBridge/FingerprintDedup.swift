import Foundation

/// Thread-safe ring-buffer dedup keyed on a string fingerprint.
public final class FingerprintDedup {
    private var seen: [String] = []
    private let maxEntries: Int
    private let queue = DispatchQueue(label: "fingerprint.dedup.serial")

    public init(maxEntries: Int = 500) {
        self.maxEntries = maxEntries
    }

    /// Returns true if this is the first time we've seen `fp`, false if duplicate.
    public func markSeen(_ fp: String) -> Bool {
        queue.sync {
            if seen.contains(fp) { return false }
            seen.append(fp)
            if seen.count > maxEntries { seen.removeFirst() }
            return true
        }
    }

    /// Stable fingerprint over the fields that identify a unique signal.
    public static func make(channel: String, author: String, body: String) -> String {
        "\(channel)|\(author)|\(body)"
    }
}
