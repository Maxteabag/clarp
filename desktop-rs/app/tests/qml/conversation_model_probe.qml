// Offscreen check of the Rust ConversationModel through a real ListView:
//   QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen \
//     CLARP_RS_QML=$PWD/app/tests/qml/conversation_model_probe.qml target/debug/clarp-desktop
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 400; height: 600; visible: true
    property int failures: 0
    property var events: []

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function row(i) { list.forceLayout(); return list.itemAtIndex(i) }

    ConversationModel {
        id: convModel
        onRowsAppended: fromUser => events.push("appended:" + fromUser)
        onRowsPrepended: events.push("prepended")
        onDeliveryConfirmed: id => events.push("confirmed:" + id)
        onReplacementRequired: events.push("replace")
        onDataChanged: events.push("changed")
    }

    ListView {
        id: list
        width: 400; height: 600
        model: convModel
        delegate: Text {
            required property string messageId
            required property string body
            required property bool pending
            required property var tools
            required property var displayCells
            property string rowId: messageId
            text: messageId + ": " + body
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
        {
            convModel.openSession("rachel")
            check(convModel.session === "rachel", "session property")
            convModel.applyLogText(JSON.stringify({conversation_id: "c1", latest_revision: 1, turns: [
                {id: "live-1", role: "assistant", kind: "live", text: "Hello", revision: 1}]}), "tail")
            check(convModel.count === 1 && list.count === 1, "tail loads one row")
            const streaming = row(0)
            convModel.applyLogText(JSON.stringify({conversation_id: "c1", latest_revision: 2, turns: [
                {id: "live-1", role: "assistant", kind: "live", text: "Hello world <spe", revision: 2}]}), "delta")
            check(streaming !== null && row(0) === streaming, "streaming update keeps the delegate")
            check(row(0).body === "Hello world", "partial voice tag hidden: " + row(0).body)
            check(events.indexOf("changed") >= 0, "dataChanged emitted")
            convModel.addOptimistic("a", "question")
            check(list.count === 2 && row(1).pending, "optimistic row appended as pending")
            check(events.indexOf("appended:true") >= 0, "rowsAppended(fromCurrentUser)")
            convModel.applyLogText(JSON.stringify({conversation_id: "c1", latest_revision: 3, turns: [
                {id: "u-a", role: "user", text: "question", revision: 3},
                {id: "m9", role: "assistant", text: "answer", revision: 4,
                 tools: [{name: "Read"}], display_cells: [{kind: "subagents", title: "Spawning agent", status: "running", summary: "Kepler"}]}]}), "delta")
            check(events.indexOf("confirmed:a") >= 0, "deliveryConfirmed(a)")
            // The C++ merge rule: a durable assistant row lands right after the
            // live row it follows, ahead of the user's bubble.
            const at = convModel.indexOfMessage("m9")
            const last = row(at)
            check(at === 1 && last.rowId === "m9", "durable reply placed after the live row: " + at)
            check(last.tools.length === 1 && last.tools[0].name === "Read", "tools role is a JS array")
            check(last.displayCells[0]._subagent.phase === "spawned", "sub-agent summary on the way out")
            check(convModel.latestRevision === 3, "latestRevision follows the response field: " + convModel.latestRevision)
            convModel.applyLogText(JSON.stringify({conversation_id: "c1", turns: [
                {id: "old", role: "assistant", text: "older", revision: 0}]}), "older")
            check(row(0) && row(0).rowId === "old" && events.indexOf("prepended") >= 0, "older page prepends")
            convModel.applyLogText(JSON.stringify({conversation_id: "c2", turns: []}), "delta")
            check(events.indexOf("replace") >= 0, "conversation change requests replacement")
            check(convModel.indexOfMessage("m9") === 2 && convModel.indexOfMessage("nope") === -1, "indexOfMessage")
        }
    }
}
