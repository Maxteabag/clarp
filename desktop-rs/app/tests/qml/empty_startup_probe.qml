// needs: fake-host
// env: CLARP_EMPTY_STARTUP=1
// Port of tst_native_core::emptyStartupWaitsForExplicitChoiceAndRetryTargetsLatestFailure.
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
    function requests() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e); stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            check(app.selectedSession === "" && app.panes.activeSession === "", "empty startup selects nothing")
            check(!requests().some(r => r.path === "/select"), "no POST /select before a choice")
            app.selectSession("rachel")
            stage = 1
        } else if (stage === 1 && app.conversation.count === 2) {
            check(app.selectedSession === "rachel" && requests().some(r => r.path === "/select"), "explicit choice selects")
            const model = app.conversationForSession("rachel")
            model.addOptimistic("older-failure", "Earlier failed text")
            model.markDeliveryFailed("older-failure")
            model.addOptimistic("latest-failure", "Latest failed text")
            model.markDeliveryFailed("latest-failure")
            const other = app.conversationForSession("other")
            other.addOptimistic("other-failure", "Other chat text")
            other.markDeliveryFailed("other-failure")
            app.retryLatestFailedMessage()
            stage = 2
        } else if (stage === 2 && requests().some(r => r.path === "/send")) {
            const send = requests().find(r => r.path === "/send")
            check(send.body.text === "Latest failed text" && send.body.session === "rachel", "retry sends the latest failure")
            check(app.conversationForSession("rachel").indexOfMessage("u-older-failure") >= 0, "older failure kept")
            check(app.conversationForSession("other").indexOfMessage("u-other-failure") >= 0, "other chat untouched")
            ticker.stop()
            finish()
        }
    }
}
