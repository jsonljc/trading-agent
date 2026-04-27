// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NotificationBridge",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "NotificationBridgeCore",
            path: "Sources/NotificationBridge",
            exclude: ["main.swift"]
        ),
        .executableTarget(
            name: "NotificationBridge",
            dependencies: ["NotificationBridgeCore"],
            path: "Sources/NotificationBridge",
            sources: ["main.swift"]
        ),
        .testTarget(
            name: "NotificationBridgeTests",
            dependencies: ["NotificationBridgeCore"],
            path: "Tests/NotificationBridgeTests"
        ),
    ]
)
