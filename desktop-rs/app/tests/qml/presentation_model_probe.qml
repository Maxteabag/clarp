// Offscreen check of the Rust ConversationPresentationModel wired like
// ConversationPane.qml: `presentation.sourceModel = conversation`.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 400; height: 800; visible: true
    property int failures: 0
    property int resets: 0
    property var appended: []

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function row(i) { list.forceLayout(); return list.itemAtIndex(i) }
    function ids() {
        list.forceLayout()
        const out = []
        for (let i = 0; i < list.count; ++i) out.push(list.itemAtIndex(i).rowId)
        return out.join(",")
    }
    function log(turns, kind) {
        conversation.applyLogText(JSON.stringify({conversation_id: "c", turns: turns}), kind)
    }
    function turn(id, role, text, extra) {
        const t = {id: id, role: role, text: text, revision: 1, timestamp: new Date().toISOString()}
        for (const k in (extra || {})) t[k] = extra[k]
        return t
    }

    ConversationModel { id: conversation }
    ConversationPresentationModel {
        id: presentation
        onModelReset: resets++
        onRowsAppended: fromUser => appended.push(fromUser)
    }
    ListView {
        id: list
        width: 400; height: 800
        model: presentation
        delegate: Text {
            required property string messageId
            required property string body
            required property string groupLabel
            required property string activityLabel
            required property var tools
            required property bool activityInline
            property string rowId: messageId
            text: messageId + " " + body
        }
    }

    Component.onCompleted: {
        try { run() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack) }
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }

    function run() {
        conversation.openSession("rachel")
        presentation.sourceModel = conversation
        check(presentation.sourceModel === conversation, "sourceModel assignment round-trips")
        log([turn("u1", "user", "Question"),
             turn("t1", "assistant", "", {tools: [{name: "Read"}], activity_count: 1}),
             turn("t2", "assistant", "", {tools: [{name: "Bash"}], activity_count: 1}),
             turn("a1", "assistant", "Answer")], "tail")
        check(ids() === "u1,t1,t2,a1", "always visible shows every row: " + ids())
        check(presentation.leadingDayLabel === "Today", "leading day label: " + presentation.leadingDayLabel)

        presentation.activityMode = 0
        check(ids() === "u1,t1,a1", "grouped mode folds the tool run: " + ids())
        check(row(1).groupLabel.indexOf("2 tool calls") === 0 && row(1).tools.length === 0, "collapsed group label, no cards")
        check(presentation.indexOfMessage("t2") === 1, "a folded row maps to its group")
        presentation.toggleGroup("t1")
        check(row(1).tools.length === 2, "expanded group carries both cards")
        presentation.activityMode = 1

        const answer = row(3)
        log([turn("live", "assistant", "Hel", {kind: "live", revision: 2})], "delta")
        check(ids() === "u1,t1,t2,a1,live", "streaming row appended: " + ids())
        check(appended.length > 0 && appended[appended.length - 1] === false, "rowsAppended(false) for a reply")
        const streaming = row(4)
        log([turn("live", "assistant", "Hello there", {kind: "live", revision: 3})], "delta")
        check(row(4) === streaming && row(4).body === "Hello there", "streamed token updates the row in place")
        check(row(3) === answer, "unrelated rows keep their delegates")

        presentation.showWhenReady = true
        check(ids() === "u1,t1,t2,a1", "ready mode hides the provisional stream: " + ids())
        check(presentation.indexOfMessage("live") === -1, "hidden stream has no index")
        log([turn("final", "assistant", "Hello there, done", {revision: 4})], "delta")
        check(ids().endsWith("final"), "final reply revealed: " + ids())
        presentation.showWhenReady = false

        conversation.addOptimistic("x", "Follow-up")
        check(appended[appended.length - 1] === true, "rowsAppended(true) for the user's own send")
        check(presentation.count === list.count, "count property follows")
        check(resets === 0, "never reset after the source was bound (" + resets + ")")
    }
}
