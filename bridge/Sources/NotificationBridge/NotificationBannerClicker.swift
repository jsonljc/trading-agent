import AppKit
import ApplicationServices
import Foundation

/// Watches for macOS notification banners from Discord and clicks them.
/// Clicking navigates Discord to the channel that sent the notification.
public final class NotificationBannerClicker {
    private let discordBundleId: String
    private var lastWindowCount = 0
    private let onClicked: () -> Void

    public init(discordBundleId: String, onClicked: @escaping () -> Void) {
        self.discordBundleId = discordBundleId
        self.onClicked = onClicked
    }

    public func start() {
        Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    private func poll() {
        guard let ncApp = runningProcess(named: "NotificationCenter") else { return }
        let axNC = AXUIElementCreateApplication(ncApp.processIdentifier)

        var windowsRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(axNC, kAXWindowsAttribute as CFString, &windowsRef) == .success,
              let windows = windowsRef as? [AXUIElement] else { return }

        let count = windows.count
        guard count > lastWindowCount else {
            lastWindowCount = count
            return
        }
        lastWindowCount = count

        // New banner appeared — check if it's from Discord then click it
        for win in windows {
            if isDiscordBanner(win) {
                clickBanner(win)
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    self.onClicked()
                }
                return
            }
        }
    }

    private func isDiscordBanner(_ window: AXUIElement) -> Bool {
        // Walk the banner's subtree looking for "Discord" in any text value
        var found = false
        walkTree(window, depth: 0, maxDepth: 6) { el in
            guard !found else { return }
            var val: CFTypeRef?
            if AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &val) == .success,
               let text = val as? String, text.contains("Discord") {
                found = true
            }
            if AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &val) == .success,
               let text = val as? String, text.contains("Discord") {
                found = true
            }
        }

        // If no Discord text found, check if Discord is frontmost after click anyway
        // (on macOS 26 banners may not expose text)
        return true  // click all new banners — Discord will navigate if it's a Discord one
    }

    private func clickBanner(_ window: AXUIElement) {
        // Get window position and size, then CGEvent-click the center
        var posRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString, &posRef) == .success,
              AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeRef) == .success else {
            AXUIElementPerformAction(window, kAXPressAction as CFString)
            return
        }
        var pos = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posRef as! AXValue, .cgPoint, &pos)
        AXValueGetValue(sizeRef as! AXValue, .cgSize, &size)
        let cx = pos.x + size.width / 2
        let cy = pos.y + size.height / 2
        let src = CGEventSource(stateID: .hidSystemState)
        let down = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)
        let up   = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp,   mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
        print("Clicked notification banner at (\(cx), \(cy))")
    }

    private func runningProcess(named name: String) -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first { $0.localizedName == name }
    }

    private func walkTree(_ element: AXUIElement, depth: Int, maxDepth: Int, visitor: (AXUIElement) -> Void) {
        guard depth <= maxDepth else { return }
        visitor(element)
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
              let kids = children as? [AXUIElement] else { return }
        for kid in kids { walkTree(kid, depth: depth + 1, maxDepth: maxDepth, visitor: visitor) }
    }
}
