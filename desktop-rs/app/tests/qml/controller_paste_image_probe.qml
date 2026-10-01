// needs: fake-host
// fixture-copy: clipboard red-3x2.png
// tst_native_core::clipboardImageBecomesAttachmentWithoutSending: a copied
// image becomes a ready attachment of the open chat, the draft stays and
// nothing is sent; text on the clipboard is left to the native paste; a
// paste into a pane that is not the open one is refused with a message.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    readonly property string dir: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17).replace(/host\.log$/, "")
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function read(url) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", url, false)
        xhr.responseType = "arraybuffer"
        xhr.send()
        return xhr.response
    }
    function bytes(buffer) { return Array.from(new Uint8Array(buffer)).join(",") }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected) {
            app.restoreDesktopSession("rachel")
            app.sharedFilesystem = true
            stage = 1
        } else if (stage === 1 && app.panes.activeSession === "rachel") {
            const pane = app.panes.activePaneId
            app.setPaneDraft(pane, "rachel", "Keep this draft")
            // The test clipboard starts out holding the image.
            const png = read(Qt.resolvedUrl("../fixtures/red-3x2.png"))
            check(app.pasteClipboardImage(pane, "rachel"), "an image is taken")
            const attachments = app.composerAttachments(pane, "rachel")
            check(attachments.length === 1 && attachments[0].status === "ready", "one ready attachment: " + JSON.stringify(attachments))
            const path = attachments.length ? attachments[0].path : ""
            check(path.endsWith(".png") && bytes(read("file://" + path)) === bytes(png), "the attachment is the pasted image")
            check(app.paneDraft(pane, "rachel") === "Keep this draft", "the draft is kept")
            check(!app.sending, "nothing is sent")
            check(app.pasteClipboardImage("wrong-pane", "rachel") && app.errorMessage === "Select a conversation before pasting an image",
                  "a paste elsewhere is refused: " + app.errorMessage)
            check(app.composerAttachments(pane, "rachel").length === 1, "and adds nothing")
            app.copyToClipboard("plain paste")
            check(!app.pasteClipboardImage(pane, "rachel"), "text on the clipboard is left to the native paste")
            app.removeComposerAttachment(pane, "rachel", attachments[0].id)
            app.setPaneDraft(pane, "rachel", "")
            ticker.stop()
            finish()
        }
    }
}
