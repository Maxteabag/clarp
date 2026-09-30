// needs: fake-host
// env: CLARP_RS_LAUNCH_MODE=1
// C++ resumeLaunchOpensExactSessionWithoutFleet: a launch that names a
// backend loads no fleet until its agent exists, shows only that agent,
// then resumes the fleet (and tst_native_core::fastLaunchOpensWithoutWaitingForFleet:
// the chosen directory reaches the Host, the pane follows the new agent, and
// a burst of refreshes costs one snapshot).
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int createdAt: 0
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
    function count(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path).length
    }
    function body(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        const rows = xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path && r.method === "POST")
        return rows.length ? rows[rows.length - 1].body : {}
    }
    property int snapshotsBefore: 0
    function event(body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected) {
            event({"type": "tts-error", "message": "Other agent speech failed"})
            stage = 1
            createdAt = ticks
        } else if (stage === 1 && ticks > createdAt + 8) {
            check(app.errorMessage === "", "a launch screen reports no other agent's speech error")
            check(count("/agents/snapshot") === 0 && count("/agent-conversations") === 0 && count("/attention") === 0, "no fleet while launching")
            app.selectSession("rachel")
            check(app.selectedSession !== "rachel", "only the launched agent can be shown")
            app.setLaunchDirectory("/work/chosen directory")
            check(app.startAnonymousAgent("codex", "", ""), "launch accepted")
            stage = 2
        } else if (stage === 2 && app.selectedSession === "anon-new") {
            check(count("/agents/snapshot") === 0, "the new agent shows before any fleet load")
            check(body("/agents").cwd === "/work/chosen directory", "the chosen directory reaches the Host: " + JSON.stringify(body("/agents")))
            check(app.panes.activeSession === "anon-new", "the pane follows the new agent")
            createdAt = ticks
            stage = 3
        } else if (stage === 3 && count("/agents/snapshot") > 0 && app.agents.count >= 2) {
            check(ticks - createdAt >= 15, "the fleet resumes about half a second later: " + (ticks - createdAt) * 25 + " ms")
            app.selectSession("rachel")
            check(app.selectedSession === "rachel", "after launch any chat can be shown")
            snapshotsBefore = count("/agents/snapshot")
            for (let i = 0; i < 20; ++i) app.refreshAgents()
            createdAt = ticks
            stage = 4
        } else if (stage === 4 && ticks > createdAt + 60) {  // past the 700 ms coalescing
            const extra = count("/agents/snapshot") - snapshotsBefore
            check(extra >= 1 && extra <= 2, "20 refreshes cost at most two snapshots: " + extra)
            ticker.stop()
            finish()
        }
    }
}
