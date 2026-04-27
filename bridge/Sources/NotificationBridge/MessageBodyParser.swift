import Foundation

public enum MessageBodyParser {
    public struct Result {
        public let author: String
        public let message: String
    }

    public static func parse(_ raw: String) -> Result {
        guard let range = raw.range(of: ": ") else {
            return Result(author: "", message: raw)
        }
        let author = raw[..<range.lowerBound].trimmingCharacters(in: .whitespaces)
        let message = String(raw[range.upperBound...])
        return Result(author: author, message: message)
    }
}
