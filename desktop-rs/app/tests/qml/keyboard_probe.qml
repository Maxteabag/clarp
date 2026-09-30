// needs: fake-host
// Opening a chat plays its clips; the silent sink keeps them from raising
// a no-device error banner that Escape would dismiss first.
// env: CLARP_AUDIO_OUTPUT=null
// C++ KeyboardSmokeCheck (CLARP_SCREENSHOT_CONTEXT_KEYBOARD) on the real
// Main: real key presses (KeyInjector, offscreen only) move between the
// composer, the pane, the sidebar, search and the quick switcher; letters
// type into the composer rather than navigate; drafts stay with their chat.
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
    KeyInjector { id: keys }
    function find(test) {
        const seen = new Set()
        const walk = (o) => {
            if (!o || seen.has(o)) return null
            seen.add(o)
            if (test(o)) return o
            for (const list of [o.data, o.children, o.resources]) {
                if (!list) continue
                for (let i = 0; i < list.length; ++i) { const f = walk(list[i]); if (f) return f }
            }
            return null
        }
        return walk(root.contentItem) || walk(root)
    }
    property var map: null
    property var rail: null
    property var ctl: null
    function press(key, modifiers) { keys.press(root, key, modifiers || 0) }
    function context() { return map.contextName }
    function focusText() { return root.activeFocusItem && root.activeFocusItem.text !== undefined ? root.activeFocusItem.text : "" }
    function control(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", ctl.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    property bool hintsBefore: true
    // Each step presses keys, then waits (up to a second) for its state.
    readonly property var steps: [
        {what: "the open chat's composer has focus", act: () => {}, until: () => context() === "composer" && ctl.selectedSession === "rachel"},
        {what: "letters type into the composer instead of navigating", act: () => keys.type(root, "ejki "), until: () => focusText() === "ejki " && context() === "composer"},
        {what: "Ctrl+Shift+K toggles the shortcut hints without leaving the composer",
         act: () => { hintsBefore = root.shortcutsVisible; press(Qt.Key_K, Qt.ControlModifier | Qt.ShiftModifier) },
         until: () => root.shortcutsVisible !== hintsBefore && context() === "composer"},
        {what: "and back", act: () => press(Qt.Key_K, Qt.ControlModifier | Qt.ShiftModifier), until: () => root.shortcutsVisible === hintsBefore},
        {what: "Escape leaves the composer for the pane", act: () => press(Qt.Key_Escape), until: () => context() === "pane"},
        {what: "E goes to the sidebar", act: () => press(Qt.Key_E), until: () => context() === "sidebar"},
        {what: "J and Enter open the next chat in its composer", act: () => { press(Qt.Key_J); press(Qt.Key_Return) },
         until: () => context() === "composer" && ctl.selectedSession === "mike"},
        {what: "its composer is its own", act: () => {}, until: () => focusText() === "" && ctl.paneDraft(ctl.panes.activePaneId, "rachel") === "ejki "},
        {what: "Ctrl+B hides the sidebar", act: () => press(Qt.Key_B, Qt.ControlModifier), until: () => !root.sidebarVisible},
        {what: "Escape then E shows it again and goes there", act: () => { press(Qt.Key_Escape); press(Qt.Key_E) },
         until: () => root.sidebarVisible && context() === "sidebar"},
        {what: "/ searches the chats", act: () => press(Qt.Key_Slash), until: () => context() === "search"},
        {what: "a search that matches nothing empties the list", act: () => keys.type(root, "zzzzzz"), until: () => context() === "search" && rail.rowCount === 0},
        {what: "Escape leaves search with every chat back", act: () => press(Qt.Key_Escape), until: () => context() === "sidebar" && rail.rowCount === 2},
        {what: "Ctrl+K opens the quick switcher", act: () => press(Qt.Key_K, Qt.ControlModifier), until: () => context() === "modal"},
        {what: "typing a name and Enter opens that chat", act: () => { keys.type(root, "Rachel"); press(Qt.Key_Return) },
         until: () => context() === "composer" && ctl.selectedSession === "rachel" && focusText() === "ejki "},
        {what: "a chat waiting for an answer is attention", act: () => control("/__control/agent", {"session": "mike", "set": {"latest_state": "waiting"}}),
         until: () => map.hasAttention},
        {what: "Escape, then N jumps to it", act: () => { press(Qt.Key_Escape); press(Qt.Key_N) },
         until: () => ctl.selectedSession === "mike" && context() === "composer"},
    ]
    property int index: -1
    property int waited: 0
    Timer {
        interval: 50; repeat: true; running: true
        onTriggered: {
            if (!map) {
                map = find(o => o.objectName === "keyboardMap")
                rail = find(o => o.objectName === "sidebarRail")
                ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined)
                if (!map || !rail || !ctl) { check(false, "found the keyboard map, rail and controller"); finish() }
                return
            }
            if (index < 0 && (!ctl.connected || rail.rowCount < 2)) return
            if (index < 0 || steps[index].until()) {
                if (index >= 0) check(true, steps[index].what)
                if (++index >= steps.length) { stop(); finish(); return }
                waited = 0
                steps[index].act()
                return
            }
            if (++waited > 20) {
                check(false, steps[index].what + " (context " + context() + ", selected " + ctl.selectedSession
                      + ", focus " + (root.activeFocusItem ? root.activeFocusItem.objectName || String(root.activeFocusItem) : "none")
                      + ", text \"" + focusText() + "\", rows " + rail.rowCount + ", window error \"" + ctl.errorMessage + "\", chat error \"" + ctl.conversationForSession(ctl.selectedSession).error + "\", voice \"" + ctl.conversationForSession(ctl.selectedSession).voiceError + "\")")
                stop()
                finish()
            }
        }
    }
}
