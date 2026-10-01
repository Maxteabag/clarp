// The C++ tst_transcript_rows model scenarios against the Rust TranscriptRows
// over a ListModel source: split parts, no resets when a message finishes,
// source rows mapped around split messages.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int resets: 0
    property int inserted: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function table(rows) {
        let text = "| n | name |\n|---|---|"
        for (let i = 0; i < rows; ++i) text += "\n| " + i + " | row " + i + " |"
        return text
    }
    function message(id, body, kind) {
        return {"messageId": id, "body": body, "messageKind": kind || "final", "authorRole": "assistant", "activity": false}
    }
    ListModel {
        id: source
        ListElement { messageId: "__seed__"; body: ""; messageKind: ""; authorRole: ""; activity: false }
    }
    TranscriptRows { id: rows }
    Connections {
        target: rows
        function onModelReset() { resets++ }
        function onRowsInserted() { inserted++ }
    }
    // Reads roles by name the way delegates see them.
    Instantiator {
        id: reader
        model: rows
        delegate: QtObject {
            required property string rowKey
            required property string fullBody
            required property int partCount
            required property string body
        }
    }
    function role(row, name) { return reader.objectAt(row)[name] }

    Timer {
        interval: 20; running: true
        onTriggered: {
            try {
                source.clear()
                source.append(message("a", "Hello."))
                source.append(message("b", table(140), "live"))
                rows.sourceModel = source
                check(rows.count === 2, "a live message is never split")
                resets = 0; inserted = 0
                source.setProperty(1, "messageKind", "final")
                check(resets === 0, "finishing does not reset")
                check(inserted >= 1 && rows.count > 9, "finishing splits into parts: " + rows.count)
                check(role(1, "rowKey") === "b#0" && role(2, "rowKey") === "b#1", "part keys")
                check(role(1, "fullBody") === table(140), "fullBody is the whole message")
                check(role(1, "partCount") === rows.count - 1, "partCount")
                check(role(2, "body").startsWith("| n | name |\n|---|---|"), "a table chunk repeats the header")

                source.clear()
                source.append(message("a", "One."))
                source.append(message("big", table(100)))
                source.append(message("c", "Three."))
                const bigParts = rows.count - 2
                check(bigParts > 1, "big table splits")
                source.insert(0, message("older", "Zero."))
                check(rows.count === bigParts + 3 && role(0, "rowKey") === "older", "insert before maps rows")
                check(rows.rowForSource(2) === 2 && rows.rowForSource(3) === 2 + bigParts, "rowForSource around a split message: " + rows.rowForSource(2) + " " + rows.rowForSource(3) + " parts " + bigParts + " sources " + [0,1,2,3,4,5].map(r => rows.sourceRow(r)).join(","))
                source.remove(2)
                check(rows.count === 3 && role(2, "rowKey") === "c" && rows.sourceRow(2) === 2, "removing the split message")
            } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack) }
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
