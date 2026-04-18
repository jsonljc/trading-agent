import Foundation

class SocketEmitter {
    private let socketPath: String

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func emit(_ event: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: event),
              var jsonString = String(data: data, encoding: .utf8) else { return }
        jsonString += "\n"

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return }
        defer { Darwin.close(fd) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: 108) { cptr in
                _ = strlcpy(cptr, socketPath, 108)
            }
        }

        let connected = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sptr in
                Darwin.connect(fd, sptr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else { return }

        _ = jsonString.withCString { Darwin.write(fd, $0, strlen($0)) }
    }
}
