// End-to-end against a real Clarp Host (tests/qa/host.py: the production
// server with a deterministic Codex provider), driven like a person would:
// the roster loads, a chat opens, a message is typed and sent through the
// composer, the reply streams in and completes, and another chat opens from
// the keyboard. Each stage is captured to CLARP_E2E_OUT. Run by run-e2e.sh.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1280; height: 800
    property int failures: 0
    readonly property string out: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--e2e-out=")) return arg.substring(10)
        return ""
    }
    readonly property string prompt: "Hello from the Rust desktop end-to-end run, please answer"
    readonly property string reply: "QA reply: " + prompt
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "E2E_PASS" : "E2E_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function walk(o, visit, seen) {
        if (!o || seen.has(o)) return
        seen.add(o)
        visit(o)
        for (const list of [o.data, o.children, o.resources]) {
            if (!list) continue
            for (let i = 0; i < list.length; ++i) walk(list[i], visit, seen)
        }
    }
    function find(test) {
        let found = null
        walk(root.contentItem, o => { if (!found && test(o)) found = o }, new Set())
        if (!found) walk(root, o => { if (!found && test(o)) found = o }, new Set())
        return found
    }
    function texts() {
        const result = []
        walk(root.contentItem, o => { if (o.objectName === "messageTextBlock" && o.visible) result.push(String(o.text)) }, new Set())
        return result
    }
    function shot(name) {
        const ok = capture.capture(root, out + "/" + name + ".png")
        check(ok, "captured " + name)
    }
    WindowCapture { id: capture }
    KeyInjector { id: keys }
    property var ctl: null
    property var map: null
    property int step: 0
    property int waited: 0
    property bool streamingShot: false
    // The partial reply is visible for ~150 ms: catch it on the frame it paints.
    onAfterAnimating: {
        if ((step !== 3 && step !== 4) || streamingShot) return
        const partial = texts().find(t => t.indexOf("QA reply") >= 0 && t.indexOf(prompt) < 0)
        if (partial !== undefined) {
            streamingShot = true
            console.log("streaming frame shows: " + partial.replace(/<[^>]*>/g, "").trim())
            shot("04-streaming")
        }
    }
    Timer {
        interval: 100; repeat: true; running: true
        onTriggered: {
            if (!ctl) {
                ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined)
                map = find(o => o.objectName === "keyboardMap")
                return
            }
            if (++waited > 300) { check(false, "timed out at step " + step + " (error \"" + ctl.errorMessage + "\", focus " + (root.activeFocusItem ? root.activeFocusItem.objectName : "none") + ", text \"" + (root.activeFocusItem && root.activeFocusItem.text !== undefined ? root.activeFocusItem.text : "") + "\")"); stop(); finish(); return }
            const model = ctl.selectedSession ? ctl.conversationForSession(ctl.selectedSession) : null
            switch (step) {
            case 0:
                if (!ctl.connected || ctl.agents.count < 2) return
                check(ctl.connectionState === "live", "live connection to the real Host")
                check(ctl.agentName("rachel") === "Rachel" && ctl.agentName("mike") === "Mike", "the roster lists the Host's agents")
                ctl.selectSession("rachel")
                step = 1; waited = 0
                return
            case 1:
                if (ctl.selectedSession !== "rachel" || !model || model.loading || waited < 10) return
                shot("01-roster")
                check(map.contextName === "composer", "the open chat's composer has focus")
                keys.type(root, prompt)
                step = 2; waited = 0
                return
            case 2:
                if (root.activeFocusItem.text !== prompt) return
                shot("02-composed")
                keys.press(root, Qt.Key_Return, 0)
                step = 3; waited = 0
                return
            case 3:
                if (!texts().some(t => t.indexOf(prompt) >= 0)) return
                check(root.activeFocusItem.text === "", "sending clears the composer")
                shot("03-sent")
                step = 4; waited = 0
                return
            case 4:
                if (!texts().some(t => t.indexOf(reply) >= 0)) return
                check(streamingShot, "the reply was seen streaming before it completed")
                step = 5; waited = 0
                return
            case 5:
                if (ctl.sending || ctl.agentState("rachel") === "thinking" || waited < 8) return
                check(ctl.errorMessage === "", "no error: \"" + ctl.errorMessage + "\"")
                const rows = []
                for (let i = 0; i < model.count; ++i) rows.push(i)
                check(model.indexOfMessage("") < 0, "rows are real")
                shot("05-replied")
                keys.press(root, Qt.Key_K, Qt.ControlModifier)
                keys.type(root, "Mike")
                keys.press(root, Qt.Key_Return, 0)
                step = 6; waited = 0
                return
            default:
                if (ctl.selectedSession !== "mike" || waited < 10) return
                check(map.contextName === "composer", "the quick switcher opened Mike's chat")
                shot("06-second-chat")
                stop()
                finish()
            }
        }
    }
}
