// Offscreen port of the sidebar cases in tst_native_core.cpp
// (sidebarNestsHelpersAndCollapsesFinishedOnes, sidebarUpdatesNeverResetTheRows,
// redesignedRosterFiltersWithoutMutatingSource) through a real ListView.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 400; height: 800; visible: true
    property int failures: 0
    property int resets: 0

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function sessions() {
        const out = []
        list.forceLayout()
        for (let i = 0; i < list.count; ++i) out.push(list.itemAtIndex(i).session)
        return out.join(",")
    }
    function row(session) { list.forceLayout(); return list.itemAtIndex(sidebar.indexOfSession(session)) }
    function rosterRow(session, id, activity, extra) {
        const row = {session: session, agent_id: id, persona: session.toUpperCase(), last_activity: activity}
        for (const k in (extra || {})) row[k] = extra[k]
        return row
    }
    function helperRow(session, parent, state, activity) {
        return rosterRow(session, session + "-id", activity, {role: "helper", parent_agent_id: parent, helper_state: state})
    }
    function snapshot(agents) { roster.applySnapshotText(JSON.stringify({agents: agents})) }

    AgentListModel { id: roster }
    AgentFilterModel {
        id: sidebar
        sourceModel: roster
        onModelReset: resets++
    }

    ListView {
        id: list
        width: 400; height: 800
        model: sidebar
        delegate: Text {
            required property string session
            required property int treeDepth
            required property var doneHelpers
            required property string lastCompletedMessage
            text: session
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
        snapshot([rosterRow("b", "b", 300), rosterRow("a", "a", 200), rosterRow("c", "c", 100)])
        check(sessions() === "b,a,c", "no hierarchy keeps recency order: " + sessions())
        check(row("b").treeDepth === 0 && row("b").doneHelpers.length === 0, "flat rows have depth 0 and no footer")
        resets = 0

        snapshot([helperRow("done1", "p", "done", 900), helperRow("live", "p", "running", 800),
                  rosterRow("other", "o", 700), helperRow("done2", "p", "reported", 600),
                  helperRow("failed", "p", "failed", 550), rosterRow("parent", "p", 500),
                  helperRow("stray", "deleted", "running", 400)])
        check(sessions() === "other,parent,live,failed,stray", "helpers nest under their parent: " + sessions())
        check(row("failed").treeDepth === 1 && row("stray").treeDepth === 0, "depths")
        const footers = row("failed").doneHelpers
        check(footers.length === 1 && footers[0].count === 2 && footers[0].parentAgentId === "p" && !footers[0].expanded,
              "two helpers done line on the last visible row")
        check(sidebar.count === 5, "count follows the proxy: " + sidebar.count)

        sidebar.toggleDoneHelpers("p")
        check(sessions() === "other,parent,live,failed,done1,done2,stray", "expanded: " + sessions())
        check(row("failed").doneHelpers[0].expanded, "footer shows expanded")
        check(sidebar.count === 7, "count after expanding: " + sidebar.count)
        sidebar.toggleDoneHelpers("p")
        check(sidebar.indexOfSession("done1") === -1, "collapsed again")
        sidebar.revealSession("done1")
        check(sidebar.indexOfSession("done1") > 0, "reveal expands the parent")
        sidebar.toggleDoneHelpers("p")

        sidebar.query = "DONE"
        check(sessions() === "done1,done2", "search is flat and finds finished helpers: " + sessions())
        check(row("done1").treeDepth === 0, "search rows are not indented")
        sidebar.query = ""
        check(sidebar.indexOfSession("done1") === -1, "clearing the search re-collapses")

        snapshot([helperRow("live", "p", "done", 800), rosterRow("parent", "p", 500)])
        check(sessions() === "parent", "state change re-nests: " + sessions())
        check(row("parent").doneHelpers[0].count === 1, "one helper done")

        snapshot([{session: "alpha", persona: "Alpha", backend: "codex", cwd: "/work/one", last_completed_message: "Completed preview"},
                  {session: "beta", persona: "Beta", backend: "claude", cwd: "/work/two"}])
        sidebar.query = "WORK/TWO"
        check(sessions() === "beta" && roster.count === 2, "search by directory, source untouched")
        sidebar.query = ""
        check(row("alpha").lastCompletedMessage === "Completed preview", "source roles pass through")
        sidebar.unreadOnly = true
        check(sidebar.count === 0, "unread scope empty")
        roster.applyEventText("notification", JSON.stringify({session: "alpha"}))
        check(sessions() === "alpha" && sidebar.count === 1, "unread row appears: " + sessions())
        roster.clearUnread("alpha")
        check(sidebar.count === 0, "cleared unread leaves the scope")
        sidebar.unreadOnly = false
        check(resets === 0, "the sidebar never reset (" + resets + ")")
    }
}
