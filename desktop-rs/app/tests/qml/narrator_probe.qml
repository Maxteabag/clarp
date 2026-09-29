// needs: fake-host
// The Rust ToolNarrator through the controller's Host client: opt-in, a
// pending-then-ready poll, cache-only lookup, and explanation runs folding
// repeated rows in the presentation model.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    readonly property var activity: ({name: "Bash", summary: "Check files", _session: "rachel"})
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function requests(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    AppController { id: app }
    ConversationModel { id: conversation }
    ConversationPresentationModel { id: presentation }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        const narrator = app.toolNarrator
        if (stage === 0 && app.connected) {
            check(narrator !== null && !narrator.enabled && narrator.status.startsWith("Off"), "narrator starts off")
            check(narrator.detailLevels.length === 5, "five audiences")
            narrator.request(activity, "", false)
            check(requests("/tool-explanations").length === 0, "off: no Host request")
            narrator.detailLevel = 3
            check(narrator.enabled && narrator.levelDescription.startsWith("Everyday"), "opt in at Plain English")
            conversation.openSession("rachel")
            conversation.applyLogText(JSON.stringify({conversation_id: "c", latest_revision: 2, turns: [
                {id: "t1", role: "assistant", text: "", revision: 1, tools: [{name: "Bash", summary: "Check files"}]},
                {id: "t2", role: "assistant", text: "", revision: 2, tools: [{name: "Bash", summary: "Check files"}]}]}), "tail")
            presentation.sourceModel = conversation
            presentation.updateExplanations(narrator, "rachel", "", false)
            check(presentation.count === 2, "no explanation yet: both rows show")
            narrator.request(activity, "", false)
            stage = 1
        } else if (stage === 1 && narrator.explanation(activity, "", false) !== "") {
            check(narrator.explanation(activity, "", false) === "Explained: Check files", "ready after a pending poll")
            const sent = requests("/tool-explanations")
            check(sent.length === 2 && sent[0].body.session === "rachel" && sent[0].body.detail_level === 3, "polled the Host twice for one activity")
            check(!("_session" in sent[0].body.items[0].activity), "the session is not repeated per item")
            presentation.updateExplanations(narrator, "rachel", "", false)
            check(presentation.count === 1, "repeated explanations fold into the first row: " + presentation.count)
            narrator.request(activity, "", false)
            stage = 2
            ticks = 0
        } else if (stage === 2 && ticks > 20) {
            check(requests("/tool-explanations").length === 2, "a cached explanation is not requested again")
            narrator.enabled = false
            presentation.updateExplanations(narrator, "rachel", "", false)
            check(presentation.count === 2, "turning it off restores every row")
            ticker.stop()
            finish()
        }
    }
}
