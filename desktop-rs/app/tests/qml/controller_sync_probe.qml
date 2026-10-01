// needs: fake-host
// REWRITE_PLAN sync behaviours end to end through the controller and the
// fake Host: older history loads a page at a time with `before` until there
// is no more; a new conversation id replaces the transcript; after an
// outage the event stream resumes with Last-Event-ID; a send the Host never
// files fails after the 20 s delivery window.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int mark: 0
    property string lastEvent: ""
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
    function control(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    AppController { id: app }
    property var rachel: app.conversationForSession("rachel")
    property var mike: app.conversationForSession("mike")
    Instantiator {
        id: rows
        model: rachel
        delegate: QtObject { required property string messageId; required property bool pending; required property bool deliveryFailed }
    }
    function row(id) { for (let i = 0; i < rows.count; ++i) if (rows.objectAt(i).messageId === id) return rows.objectAt(i); return null }
    Timer {
        id: ticker
        interval: 50; repeat: true; running: true
        onTriggered: {
            if (++ticks > (stage < 7 ? 200 : 900)) { check(false, "timed out at stage " + stage + " (mike " + mike.count + " more " + mike.hasMore + " selected " + app.selectedSession + " logs " + JSON.stringify(requests("/log").map(r => r.query)).substring(0, 600) + ")"); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.selectedSession === "rachel" && rachel.count > 0) {
            // A long history, opened fresh: the tail is paged.
            control("/__control/fill", {session: "mike", count: 250, conversation_id: "c-mike-long"})
            app.selectSession("mike")
            stage = 1
        } else if (stage === 1 && mike.count === 100 && mike.indexOfMessage("mike-249") >= 0) {
            check(mike.hasMore && mike.indexOfMessage("mike-150") === 0, "the newest page of 100 loads, with more before it")
            app.loadOlderSession("mike")
            stage = 2
        } else if (stage === 2 && mike.count < 200) {
            app.loadOlderSession("mike")  // ignored while another log request is in flight, like a scroll
        } else if (stage === 2 && mike.count === 200) {
            const older = requests("/log").filter(r => r.query.before)
            check(older.length >= 1 && older.every(r => r.query.before === "mike-150"), "the next page asks for rows before the oldest held: " + JSON.stringify(older.map(r => r.query.before)))
            check(mike.indexOfMessage("mike-50") === 0 && mike.hasMore, "it lands above the rows already shown")
            app.loadOlderSession("mike")
            stage = 3
        } else if (stage === 3 && mike.count < 250) {
            app.loadOlderSession("mike")
        } else if (stage === 3 && mike.count === 250) {
            check(!mike.hasMore && mike.indexOfMessage("mike-0") === 0, "the last page ends the history")
            app.loadOlderSession("mike")
            app.selectSession("rachel")
            mark = requests("/log").length
            control("/__control/fill", {session: "rachel", count: 5, prefix: "Fresh", conversation_id: "c-rachel-2"})
            stage = 4
        } else if (stage === 4 && rachel.conversationId === "c-rachel-2" && rachel.count === 5) {
            check(requests("/log").slice(mark).every(r => !r.query.before), "no request past the end of history")
            check(rachel.indexOfMessage("rachel-0") === 0 && rachel.indexOfMessage("rachel-249") < 0 && !rachel.hasMore,
                  "a new conversation id replaces the transcript instead of merging into it")
            mark = requests("/events").length
            control("/__control/event", {type: "agent-roster", session: "rachel", kind: "updated"})
            control("/__control/outage", {seconds: 2})
            stage = 5
        } else if (stage === 5 && requests("/events").length > mark) {
            const resumed = requests("/events").slice(mark)
            check(resumed.every(r => /^[0-9]+$/.test(r.last_event_id)), "the stream resumes with Last-Event-ID: " + JSON.stringify(resumed.map(r => r.last_event_id)))
            stage = 6
        } else if (stage === 6 && app.connected) {
            check(app.connectionState === "live", "live again after the outage")
            app.sendMessageTo("rachel", "never-file", false)
            mark = ticks
            stage = 7
        } else if (stage === 7) {
            const unfiled = []
            for (let i = 0; i < rows.count; ++i) if (rows.objectAt(i).messageId.startsWith("u-")) unfiled.push(rows.objectAt(i))
            if (unfiled.length === 0) return
            const sent = unfiled[unfiled.length - 1]
            if ((ticks - mark) * 50 < 18000) {
                if (!sent.pending || sent.deliveryFailed) { check(false, "the send fails before its 20 s window"); ticker.stop(); finish() }
                return
            }
            if (!sent.deliveryFailed) return
            check(!sent.pending && !app.sending, "a send the Host never files fails after its 20 s window: " + ((ticks - mark) * 50) + " ms")
            ticker.stop()
            finish()
        }
    }
}
