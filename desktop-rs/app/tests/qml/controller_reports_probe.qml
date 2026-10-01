// needs: fake-host
// Artifacts and reports: a chat's artifacts, reports the desktop shows itself
// (Markdown as is, HTML sanitised so it loads nothing remote), tool calls
// fetched when a row opens them, and a voice preview request.
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
    function requests(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path)
    }
    AppController { id: app }
    // Reads rows by role name, the way delegates see them.
    Instantiator {
        id: rows
        model: app.conversationForSession("rachel")
        delegate: QtObject { required property string messageId; required property var tools }
    }
    function toolsOf(id) {
        for (let i = 0; i < rows.count; ++i) if (rows.objectAt(i).messageId === id) return rows.objectAt(i).tools
        return []
    }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.updateArtifacts.length === 4) {
            check(app.artifactsForSession("rachel").map(a => a.artifact_id).join() === "doc1,html1", "a chat's artifacts")
            const doc = app.reportForArtifact("doc1")
            check(doc.isHtml === false && doc.body === "# Findings\n\nAll *good*." && doc.title === "Findings" && doc.session === "rachel", "Markdown report as written")
            const page = app.reportForArtifact("html1")
            check(page.isHtml === true && page.body.indexOf("tracker.example") < 0, "HTML report loads nothing remote: " + page.body)
            check(Object.keys(app.reportForArtifact("link1")).length === 0 && Object.keys(app.reportForArtifact("nope")).length === 0, "no report for links or unknown ids")
            check(app.artifactIsViewableReport({"type": "research", "content": "x"}) && !app.artifactIsViewableReport({"type": "document", "content": "  "}), "viewable reports")
            app.loadMessageToolDetails("rachel", "r2")
            app.loadMessageToolDetails("rachel", "r2")
            app.previewVoice("rachel", "Rachel", "v1")
            stage = 1
        } else if (stage === 1 && toolsOf("r2").length === 1) {
            check(toolsOf("r2")[0].name === "Bash", "the row gained its tool calls")
            check(requests("/message-tool-details").length === 1, "tool details fetched once while in flight")
            const preview = requests("/preview")[0].body
            check(preview.voice_id === "v1" && preview.text === "Hi, I'm Rachel." && preview.session === "rachel", "voice preview request")
            ticker.stop()
            finish()
        }
    }
}
