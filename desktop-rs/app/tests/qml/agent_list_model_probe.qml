// Offscreen check of the Rust AgentListModel through a real ListView.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 400; height: 600; visible: true
    property int failures: 0
    property int moves: 0
    property int resets: 0
    property int counts: 0

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function row(i) { list.forceLayout(); return list.itemAtIndex(i) }
    function agent(id, session, activity) {
        return {agent_id: id, session: session, persona: session.toUpperCase(), last_activity: activity,
                latest_state: "idle", schedules: [{schedule_id: "s"}]}
    }

    AgentListModel {
        id: roster
        onRowsMoved: moves++
        onModelReset: resets++
        onCountChanged: counts++
    }

    ListView {
        id: list
        width: 400; height: 600
        model: roster
        delegate: Text {
            required property string session
            required property string name
            required property string agentState
            required property bool busy
            required property bool unread
            required property var schedules
            required property int processCount
            text: name + " " + agentState
        }
    }

    Timer {
        interval: 0; running: true
        onTriggered: {
            try { run() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack) }
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }

    function run() {
        roster.applySnapshotText(JSON.stringify({agents: [agent("a", "rachel", 100), agent("b", "mike", 200),
            {session: "old", archived_at: 1}]}))
        check(roster.count === 2 && list.count === 2, "archived agent filtered")
        check(row(0).session === "mike", "most recent first")
        check(row(0).schedules.length === 1, "schedules role is a JS array")
        const rachel = row(1)
        check(rachel !== null && rachel.session === "rachel", "rachel delegate exists")

        roster.applySnapshotText(JSON.stringify({agents: [agent("a", "rachel", 300), agent("b", "mike", 200)]}))
        check(moves === 1 && resets === 0, "reorder is one move, no reset (moves=" + moves + ")")
        check(row(0) === rachel, "moved row keeps its delegate")

        roster.applyEventText("state", JSON.stringify({session: "rachel", kind: "thinking", ts: 500}))
        check(row(0).busy && row(0).agentState === "thinking", "state event")
        roster.applyEventText("notification", JSON.stringify({session: "mike"}))
        check(row(1).unread, "notification marks unread")
        check(roster.indexOfSession("mike") === 1 && roster.indexOfSession("x") === -1, "indexOfSession")

        check(roster.recordOutgoingActivity("mike") && row(0).session === "mike", "outgoing activity moves to top")
        check(moves === 2, "second move")

        roster.markTransportUnavailable()
        check(row(0).agentState === "offline" && !row(1).busy, "transport unavailable shows offline")

        // A downward move (from < to) needs Qt's destination = to + 1.
        roster.applySnapshotText(JSON.stringify({agents: [agent("a", "rachel", 900), agent("b", "mike", 800), agent("d", "dan", 700)]}))
        const top = row(0)
        check(top.session === "rachel", "rachel on top before moving down")
        roster.applySnapshotText(JSON.stringify({agents: [agent("a", "rachel", 900), agent("b", "mike", 1000), agent("d", "dan", 950)]}))
        const order = [row(0).session, row(1).session, row(2).session].join(",")
        check(order === "mike,dan,rachel", "downward move lands last: " + order)
        check(row(2) === top, "downward move keeps the delegate")

        const before = counts
        roster.applySnapshotText(JSON.stringify({agents: [agent("c", "carol", 900)]}))
        check(roster.count === 1 && row(0).session === "carol" && counts > before, "removal and insert change count")
        check(resets === 0, "never reset")
    }
}
