// needs: fake-host
// REWRITE_PLAN "Stop a running turn and represent queued, waiting,
// interrupted, and active states accurately": Stop asks the Host to stop
// the open chat's turn, and each state the Host reports shows on the
// agent's row as that state, busy only while it is active.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function posts(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path && r.method === "POST").map(r => r.body)
    }
    function state(kind) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify({"type": "agent-state", "session": "rachel", "kind": kind}))
    }
    AppController { id: app }
    Instantiator {
        id: rows
        model: app.agents
        delegate: QtObject { required property string session; required property string agentState; required property bool busy }
    }
    function rachel() {
        for (let i = 0; i < rows.count; ++i) if (rows.objectAt(i).session === "rachel") return rows.objectAt(i)
        return null
    }
    // Each state the Host reports, and whether the row is busy in it.
    readonly property var states: [["thinking", true], ["tool", true], ["interrupted", false], ["waiting", false], ["compacting", true], ["done", false]]
    property int next: 0
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && rachel() !== null) {
            app.selectSession("rachel")
            state(states[0][0])
            stage = 1
        } else if (stage === 1 && rachel().agentState === states[next][0]) {
            check(rachel().busy === states[next][1], states[next][0] + (states[next][1] ? " is busy" : " is not busy"))
            if (next === 0) {
                app.stopAgent()
                stage = 2
                return
            }
            if (++next < states.length) state(states[next][0])
            else { ticker.stop(); finish() }
        } else if (stage === 2 && posts("/stop").length === 1) {
            check(posts("/stop")[0].session === "rachel", "Stop asks the Host to stop the open chat: " + JSON.stringify(posts("/stop")))
            stage = 1
            next = 1
            state(states[next][0])
        }
    }
}
