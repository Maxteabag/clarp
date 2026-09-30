// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// REWRITE_PLAN "tool visibility" on the real Main: Ctrl+Shift+T shows a
// reply's tool calls as cards and hides them again, and the choice sticks.
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
    function visibleCards() {
        let n = 0
        walk(root.contentItem, o => { if (o.objectName === "toolCard" && o.visible && o.width > 0 && o.height > 0) n++ }, new Set())
        return n
    }
    KeyInjector { id: keys }
    property var ctl: null
    property int step: 0
    property int waited: 0
    property bool before: false
    Timer {
        interval: 100; repeat: true; running: true
        onTriggered: {
            if (!ctl) { ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined); return }
            if (++waited > 100) { check(false, "timed out at step " + step + " (cards " + visibleCards() + ", toolsVisible " + ctl.toolsVisible + ")"); stop(); finish(); return }
            const model = ctl.conversationForSession("rachel")
            switch (step) {
            case 0:
                if (!ctl.connected || ctl.selectedSession !== "rachel" || model.count === 0) return
                model.applyLogText(JSON.stringify({conversation_id: model.conversationId, latest_revision: 3, turns: [
                    {id: "r3", role: "assistant", text: "I checked the build.", revision: 3, timestamp: "2026-09-29T10:01:00Z",
                     tools: [{name: "Bash", summary: "cargo build"}, {name: "Read", summary: "Cargo.toml"}]}]}), "delta")
                step = 1; waited = 0
                return
            case 1:
                if (model.indexOfMessage("r3") < 0 || waited < 5) return
                before = ctl.toolsVisible
                check(!before && visibleCards() === 0, "tool calls start folded away: " + visibleCards())
                keys.press(root, Qt.Key_T, Qt.ControlModifier | Qt.ShiftModifier)
                step = 2; waited = 0
                return
            case 2:
                if (visibleCards() < 2) return
                check(ctl.toolsVisible, "Ctrl+Shift+T shows the tool calls as cards: " + visibleCards())
                keys.press(root, Qt.Key_T, Qt.ControlModifier | Qt.ShiftModifier)
                step = 3; waited = 0
                return
            default:
                if (visibleCards() > 0) return
                check(!ctl.toolsVisible, "and hides them again")
                stop()
                finish()
            }
        }
    }
}
