// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// After Escape leaves the composer, the keyboard still reads the chat: Up
// and Page Up scroll the transcript (and stop following), Home goes to the
// top of what is loaded, End returns to the latest and follows again.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1100; height: 700
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
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
    KeyInjector { id: keys }
    property var ctl: null
    property var map: null
    property var transcript: null
    property int step: 0
    property int waited: 0
    property real y0: 0
    Timer {
        interval: 100; repeat: true; running: true
        onTriggered: {
            if (!ctl) {
                ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined)
                map = find(o => o.objectName === "keyboardMap")
                return
            }
            if (++waited > 100) { check(false, "timed out at step " + step + " (context " + map.contextName + ")"); stop(); finish(); return }
            switch (step) {
            case 0: {
                if (!ctl.connected || ctl.selectedSession !== "rachel" || ctl.conversationForSession("rachel").count === 0) return
                const xhr = new XMLHttpRequest()
                xhr.open("POST", ctl.baseUrl + "/__control/fill", false)
                xhr.setRequestHeader("Content-Type", "application/json")
                xhr.send(JSON.stringify({session: "rachel", count: 60, prefix: "A longer line of transcript text so the chat scrolls"}))
                step = 1; waited = 0
                return
            }
            case 1:
                transcript = find(o => o.objectName === "transcriptList" && o.visible && o.width > 0)
                if (!transcript || ctl.conversationForSession("rachel").count < 60 || !transcript.atLatest || waited < 5) return
                check(map.contextName === "composer", "the chat opens with the composer focused")
                keys.press(root, Qt.Key_Escape, 0)
                step = 2; waited = 0
                return
            case 2:
                if (map.contextName !== "pane") return
                y0 = transcript.contentY
                keys.press(root, Qt.Key_Up, 0)
                step = 3; waited = 0
                return
            case 3:
                if (transcript.contentY >= y0) return
                check(!transcript.followLatest, "Up after Escape scrolls the chat up and stops following (" + Math.round(y0 - transcript.contentY) + " px)")
                y0 = transcript.contentY
                keys.press(root, Qt.Key_PageUp, 0)
                step = 4; waited = 0
                return
            case 4:
                if (transcript.contentY >= y0 - 100) return
                check(true, "Page Up scrolls a screen (" + Math.round(y0 - transcript.contentY) + " px)")
                keys.press(root, Qt.Key_Home, 0)
                step = 5; waited = 0
                return
            case 5:
                if (transcript.contentY > 1) return
                check(map.contextName === "pane", "Home reaches the top and the keyboard stays on the chat")
                keys.press(root, Qt.Key_End, 0)
                step = 6; waited = 0
                return
            default:
                if (!transcript.atLatest || !transcript.followLatest) return
                check(true, "End returns to the latest and follows again")
                stop()
                finish()
            }
        }
    }
}
