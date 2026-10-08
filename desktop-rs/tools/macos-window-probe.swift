// What macOS sees of a running Clarp window: CoreGraphics' window list and
// the Accessibility API's windows for the process. CI runs it after
// launching Clarp.app: swift desktop-rs/tools/macos-window-probe.swift PID
import ApplicationServices
import Foundation

let pid = pid_t(CommandLine.arguments[1])!
let listed = (CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? [])
    .filter { ($0[kCGWindowOwnerPID as String] as? pid_t) == pid && ($0[kCGWindowLayer as String] as? Int) == 0 }
print("CGWindowList windows: \(listed.count) \(listed.map { $0[kCGWindowBounds as String] ?? "" })")
print("AXIsProcessTrusted: \(AXIsProcessTrusted())")
let app = AXUIElementCreateApplication(pid)
var windows: CFTypeRef?
let status = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windows)
print("AX windows: status \(status.rawValue), count \((windows as? [AXUIElement])?.count ?? -1)")
var role: CFTypeRef?
AXUIElementCopyAttributeValue(app, kAXRoleAttribute as CFString, &role)
print("AX app role: \(role as? String ?? "none")")
