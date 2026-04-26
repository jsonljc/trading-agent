import AppKit
import ApplicationServices
import Foundation

/// Dual-mode Discord AX bridge.
/// Event mode: AXObserver callbacks on kAXValueChangedNotification.
/// Reconciliation mode: 5-second sweep of visible message pane.
final class AXDiscordWatcher {
    private let bundleId: String
    private let watchedChannels: Set<String>
    private let emitter: SocketEmitter
    private let logPath: String

    // Ring buffer — dedup across both modes. All accesses serialised through fingerprintQueue.
    private var seenFingerprints: [String] = []
    private let maxSeen = 200
    private let fingerprintQueue = DispatchQueue(label: "ax.fingerprint.serial")

    init(bundleId: String, watchedChannels: [String], socketPath: String, logPath: String) {
        self.bundleId = bundleId
        self.watchedChannels = Set(watchedChannels.map { $0.lowercased() })
        self.emitter = SocketEmitter(socketPath: socketPath)
        self.logPath = logPath
    }

    func triggerReconcile() {
        guard let app = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId).first else { return }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        reconcile(axApp: axApp)
    }

    func start() {
        // Also poll window title every 0.5s — catches user clicking a notification banner
        var lastTitle = ""
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self else { return }
            guard let app = NSRunningApplication
                    .runningApplications(withBundleIdentifier: self.bundleId).first else { return }
            let axApp = AXUIElementCreateApplication(app.processIdentifier)
            var windowsRef: CFTypeRef?
            guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
                  let windows = windowsRef as? [AXUIElement], let win = windows.first else { return }
            var titleRef: CFTypeRef?
            guard AXUIElementCopyAttributeValue(win, kAXTitleAttribute as CFString, &titleRef) == .success,
                  let title = titleRef as? String, title != lastTitle else { return }
            lastTitle = title
            let ch = self.activeChannel() ?? ""
            print("Window title changed: '\(title)' -> channel='\(ch)'")
            guard self.watchedChannels.contains(ch) else { return }
            // Title changed to a watched channel — capture immediately
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.reconcile(axApp: axApp)
            }
        }
        // Require Accessibility permission
        let opts = [kAXTrustedCheckOptionPrompt.takeRetainedValue() as String: true] as CFDictionary
        guard AXIsProcessTrustedWithOptions(opts) else {
            fputs("FATAL: Accessibility permission not granted.\n"
                + "Enable: System Settings → Privacy & Security → Accessibility → grant this terminal/app.\n", stderr)
            Foundation.exit(1)
        }

        guard let app = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId).first else {
            fputs("Discord (\(bundleId)) is not running. Start Discord then retry.\n", stderr)
            Foundation.exit(1)
        }

        let pid = app.processIdentifier
        let axApp = AXUIElementCreateApplication(pid)

        // Create AXObserver
        var obs: AXObserver?
        let selfRef = Unmanaged.passRetained(self).toOpaque()
        let createResult = AXObserverCreate(pid, { _, element, notification, refcon in
            guard let refcon else { return }
            let w = Unmanaged<AXDiscordWatcher>.fromOpaque(refcon).takeUnretainedValue()
            w.handleCallback(element: element, notification: notification as String)
        }, &obs)

        guard createResult == .success, let obs else {
            fputs("AXObserverCreate failed: \(createResult.rawValue)\n", stderr)
            Foundation.exit(1)
        }

        // Register kAXValueChangedNotification — primary
        let valueResult = AXObserverAddNotification(
            obs, axApp, kAXValueChangedNotification as CFString, selfRef)
        switch valueResult {
        case .success:
            print("Registered kAXValueChangedNotification")
        case .notificationUnsupported:
            fputs("kAXValueChangedNotification unsupported — reconciliation mode only\n", stderr)
        default:
            fputs("AXObserverAddNotification (value) returned: \(valueResult.rawValue)\n", stderr)
        }

        // Also register focus changed as secondary source
        let focusResult = AXObserverAddNotification(
            obs, axApp, kAXFocusedUIElementChangedNotification as CFString, selfRef)
        if focusResult == .success {
            print("Registered kAXFocusedUIElementChangedNotification (secondary)")
        }

        CFRunLoopAddSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(obs), .defaultMode)

        // Reconciliation timer — 5-second sweep
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.reconcile(axApp: axApp)
        }

        print("AXDiscordWatcher running. bundle=\(bundleId) channels=\(watchedChannels.sorted())")
        CFRunLoopRun()  // blocks
    }

    // MARK: - Event mode

    private func handleCallback(element: AXUIElement, notification: String) {
        let logEntry: [String: String] = [
            "type": "ax_callback",
            "notification": notification,
            "ts": isoNow(),
        ]
        appendLog(logEntry)

        guard let msg = extractMessage(from: element) else { return }
        guard watchedChannels.contains(msg.channel) else { return }
        let fp = fingerprint(channel: msg.channel, author: msg.author, body: msg.body)
        guard markSeen(fp) else { return }
        emit(channel: msg.channel, author: msg.author, body: msg.body, source: "ax_event")
    }

    // MARK: - Reconciliation mode

    private func reconcile(axApp: AXUIElement) {
        let ch = activeChannel() ?? "nil"
        print("reconcile: activeChannel='\(ch)' watched=\(watchedChannels.sorted())")
        var seen = 0
        walkTree(axApp, depth: 0, maxDepth: 8) { element in
            guard seen < 10 else { return }
            let role = axRole(of: element) ?? ""
            guard role == (kAXStaticTextRole as String) || role == (kAXTextAreaRole as String) else { return }
            let body = stringValue(of: element) ?? ""
            guard body.count > 25 else { return }
            // Skip UI chrome: channel headers, window titles, server names
            guard !body.contains("Stock Talk Insiders") else { return }
            guard !body.contains("丨") else { return }
            guard !body.hasPrefix("#") else { return }
            guard self.watchedChannels.contains(ch) else { return }
            let fp = self.fingerprint(channel: ch, author: "reconcile", body: body)
            guard self.markSeen(fp) else { return }
            self.emit(channel: ch, author: "reconcile", body: body, source: "reconciliation")
            seen += 1
        }
    }

    // MARK: - AX tree helpers

    private struct Message {
        let channel: String; let author: String; let body: String
    }

    private func extractMessage(from element: AXUIElement) -> Message? {
        let body = stringValue(of: element) ?? ""
        guard body.count > 25 else { return nil }
        let ch = activeChannel() ?? ""
        let author = authorFromParent(of: element) ?? "unknown"
        return Message(channel: ch.lowercased(), author: author, body: body)
    }

    private func activeChannel() -> String? {
        guard let app = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId).first else { return nil }
        let axApp2 = AXUIElementCreateApplication(app.processIdentifier)

        // Read channel from window title: "#channel-name | Server - Discord"
        var windowList: CFTypeRef?
        guard AXUIElementCopyAttributeValue(axApp2, kAXWindowsAttribute as CFString, &windowList) == .success,
              let windows = windowList as? [AXUIElement],
              let win = windows.first else { return nil }

        var titleRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(win, kAXTitleAttribute as CFString, &titleRef) == .success,
              let title = titleRef as? String else { return nil }

        // Title format: "#📰丨channel-name | Server - Discord"
        // Extract text before the first " | "
        let channelPart = title.components(separatedBy: " | ").first ?? title
        // Strip leading "#" and any emoji/special chars before the actual name
        // Find the last segment after "丨" or just strip "#"
        var name = channelPart
        if let idx = name.lastIndex(of: "丨") {
            name = String(name[name.index(after: idx)...])
        } else {
            name = name.trimmingCharacters(in: .init(charactersIn: "#"))
        }
        return name.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: " ", with: "-")
            .lowercased()
    }

    private func authorFromParent(of element: AXUIElement) -> String? {
        var parent: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXParentAttribute as CFString, &parent) == .success else { return nil }
        let parentEl = parent as! AXUIElement
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(parentEl, kAXChildrenAttribute as CFString, &children) == .success,
              let kids = children as? [AXUIElement] else { return nil }
        return kids.compactMap { stringValue(of: $0) }.first
    }

    private func walkTree(_ element: AXUIElement, depth: Int, maxDepth: Int, visitor: (AXUIElement) -> Void) {
        guard depth <= maxDepth else { return }
        visitor(element)
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
              let kids = children as? [AXUIElement] else { return }
        for kid in kids { walkTree(kid, depth: depth + 1, maxDepth: maxDepth, visitor: visitor) }
    }

    private func axRole(of element: AXUIElement) -> String? {
        axAttribute(of: element, key: kAXRoleAttribute as CFString) as? String
    }

    private func axAttribute(of element: AXUIElement, key: CFString) -> Any? {
        var val: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, key, &val) == .success else { return nil }
        return val
    }

    private func stringValue(of element: AXUIElement) -> String? {
        (axAttribute(of: element, key: kAXValueAttribute as CFString) as? String)
            ?? (axAttribute(of: element, key: kAXTitleAttribute as CFString) as? String)
    }

    // MARK: - Dedup

    private func fingerprint(channel: String, author: String, body: String) -> String {
        let input = "\(channel):\(author):\(body.prefix(120))"
        var h: UInt64 = 5381
        for b in input.utf8 { h = h &* 31 &+ UInt64(b) }
        return String(format: "%016llx", h)
    }

    private func markSeen(_ fp: String) -> Bool {
        fingerprintQueue.sync {
            guard !seenFingerprints.contains(fp) else { return false }
            seenFingerprints.append(fp)
            if seenFingerprints.count > maxSeen { seenFingerprints.removeFirst() }
            return true
        }
    }

    // MARK: - Emit + Log

    private func emit(channel: String, author: String, body: String, source: String) {
        let event: [String: String] = [
            "event_id": UUID().uuidString,
            "source": source,
            "channel": channel,
            "author": author,
            "trigger_preview": body,
            "received_at": isoNow(),
        ]
        emitter.emit(event)
        print("[\(source)] #\(channel) \(author): \(body.prefix(60))")
    }

    private func appendLog(_ entry: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: entry),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let url = URL(fileURLWithPath: logPath)
        if let fh = try? FileHandle(forWritingTo: url) {
            fh.seekToEndOfFile()
            fh.write(line.data(using: .utf8)!)
            try? fh.close()
        } else {
            try? line.data(using: .utf8)!.write(to: url)
        }
    }

    private func isoNow() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}
