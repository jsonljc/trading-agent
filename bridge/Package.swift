// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NotificationBridge",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "NotificationBridge",
            path: "Sources/NotificationBridge"
        )
    ]
)
