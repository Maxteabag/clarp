// needs: fake-host
// The Rust AppController against app/tests/fake_host.py: connect, snapshot,
// auto-select, log sync, send with confirmation, SSE-driven delta, stop,
// delivery timeout. Port of tst_native_core::appControllerCompletesCoreProtocolFlow.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var notifications: []
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function requests() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
    }
    function ids(model) {
        const out = []
        for (let i = 0; i < model.count; ++i) out.push(model.data(model.index(i, 0), 257))
        return out
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }

    AppController {
        id: app
        onNotificationRequested: (title, body) => notifications.push(title + ":" + body)
    }

    Timer {
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); stop(); finish() }
        }
    }

    function step() {
        if (stage === 0 && app.connected && app.conversation && app.conversation.count === 2) {
            check(app.serverName === "Fake Host" && app.serverVersion === "9.9.9", "server info")
            check(app.connectionState === "live", "connection state live")
            check(app.agents.count === 2 && app.contacts.count === 1, "roster and contacts from the snapshot")
            check(app.selectedSession === "rachel" && app.selectedName === "Rachel", "first agent auto-selected")
            check(app.panes.activeSession === "rachel", "pane follows the selection")
            const sent = requests()
            check(sent.some(r => r.path === "/select" && r.body.session === "rachel"), "POST /select for the selection")
            check(sent.some(r => r.path === "/log" && r.query.session === "rachel" && !("after_revision" in r.query)), "tail /log")
            app.sendMessage("  ping  ", false)
            check(app.sending && app.conversation.count === 3, "optimistic row while sending")
            stage = 1
        } else if (stage === 1 && !app.sending && app.conversation.count === 4) {
            const rows = ids(app.conversation)
            check(rows[2].startsWith("u-") && rows[3].startsWith("reply-"), "confirmed by id, reply merged: " + rows)
            const send = requests().find(r => r.path === "/send")
            check(send.body.text === "ping" && send.body.client_msg_id && rows[2] === "u-" + send.body.client_msg_id, "send body")
            check(requests().some(r => r.path === "/log" && r.query.after_revision), "delta /log after SSE")
            check(app.agents.data(app.agents.index(0, 0), 257 + 1) === "rachel", "outgoing chat stays on top")
            check(notifications.length === 0, "no notification for the open chat")
            app.selectSession("mike")
            stage = 2
        } else if (stage === 2 && app.selectedSession === "mike" && app.conversation.count === 2) {
            check(ids(app.conversation).join() === "m1,m2", "switching loads mike's log")
            check(app.conversationForSession("rachel").count === 4, "rachel's transcript is kept")
            app.stopAgent()
            app.sendMessage("never-file", false)
            stage = 3
        } else if (stage === 3 && requests().some(r => r.path === "/stop" && r.body.session === "mike")) {
            check(true, "POST /stop for the selected agent")
            check(app.sending, "an unfiled send stays pending")
            stage = 4
            finish()
        }
    }
}
