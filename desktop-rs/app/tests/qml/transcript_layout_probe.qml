// The C++ tst_transcript_layout scene and scenarios against the Rust
// TranscriptLayout item: a ListModel, a delegate with plain `model` and
// `index` properties, rows created only near the viewport.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    id: win
    width: 320; height: 240; visible: true
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function near(actual, expected, what) {
        check(Math.abs(actual - expected) <= 1, what + ": actual=" + actual + " expected=" + expected)
    }

    // Seeded so the ListModel has its roles when the layout reads them, and
    // cleared on completion, exactly like the C++ scene.
    ListModel {
        id: rows
        ListElement { messageId: "__seed__"; rowHeight: 1 }
    }
    Component.onCompleted: rows.clear()
    Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: layout.contentHeight
        boundsBehavior: Flickable.StopAtBounds
        clip: true
        TranscriptLayout {
            id: layout
            width: flick.width
            flickable: flick
            model: rows
            cacheExtent: 120
            creationBudget: 0
            estimateRole: "messageId"
            delegate: Component {
                Rectangle {
                    property int index: -1
                    property var model: null
                    property string messageId: model ? model.messageId : ""
                    width: layout.width
                    height: model ? model.rowHeight : 0
                    color: "transparent"
                }
            }
        }
    }

    function settle() { for (let i = 0; i < 4; ++i) layout.layoutNow() }
    function appendRows(count, prefix, height) {
        for (let i = 0; i < count; ++i) rows.append({"messageId": prefix + i, "rowHeight": height})
        settle()
    }
    function indexOf(id) {
        for (let i = 0; i < rows.count; ++i) if (rows.get(i).messageId === id) return i
        return -1
    }
    function screenOffset(row) { return layout.positionOf(row) - flick.contentY }
    function fresh() { layout.following = true; rows.clear(); flick.contentY = 0; settle() }

    function following() {
        fresh(); appendRows(20, "m", 30)
        near(flick.contentY, layout.endY(), "initial following end")
        appendRows(5, "a", 32)
        near(flick.contentY, layout.endY(), "following after append")
        rows.setProperty(22, "rowHeight", 90); settle()
        near(flick.contentY, layout.endY(), "following after row growth")
        for (let i = 0; i < 3; ++i) rows.remove(4)
        settle()
        near(flick.contentY, layout.endY(), "following after removals")
    }
    function anchor() {
        fresh(); appendRows(80, "m", 30)
        layout.positionAtRow(40, false); settle()
        const offset = screenOffset(indexOf("m40"))
        rows.insert(10, {"messageId": "inserted", "rowHeight": 44}); settle()
        near(screenOffset(indexOf("m40")), offset, "insert above anchor")
        rows.setProperty(5, "rowHeight", 140); settle()
        near(screenOffset(indexOf("m40")), offset, "grow above anchor")
        rows.setProperty(5, "rowHeight", 18); settle()
        near(screenOffset(indexOf("m40")), offset, "shrink above anchor")
        for (let i = 299; i >= 0; --i) rows.insert(0, {"messageId": "p" + i, "rowHeight": 21})
        settle()
        near(screenOffset(indexOf("m40")), offset, "prepend many rows")
        for (let i = 0; i < 30; ++i) rows.remove(20)
        settle()
        near(screenOffset(indexOf("m40")), offset, "remove rows above anchor")
    }
    function reset() {
        fresh(); appendRows(60, "m", 30)
        layout.positionAtRow(30, false); settle()
        const offset = screenOffset(indexOf("m30"))
        rows.clear()
        for (let i = 0; i < 70; ++i) rows.append({"messageId": i === 44 ? "m30" : "r" + i, "rowHeight": 34})
        settle()
        check(indexOf("m30") === 44, "anchor message moved to row 44")
        near(screenOffset(44), offset, "clear and re-append keeps the same message")
    }
    function widthChange() {
        fresh(); appendRows(100, "m", 30)
        layout.positionAtRow(50, false); settle()
        win.width = 520; settle()
        const offset = screenOffset(indexOf("m50"))
        check(offset >= -1 && offset <= 239, "anchored row remains on screen after a width change, offset=" + offset)
        win.width = 320; settle()
    }
    function neighbours() {
        fresh(); appendRows(120, "m", 30)
        layout.positionAtRow(70, false); settle()
        near(flick.contentY, layout.positionOf(70), "row positioned at top")
        rows.setProperty(69, "rowHeight", 105); rows.setProperty(71, "rowHeight", 64); settle()
        near(flick.contentY, layout.positionOf(70), "row remains at top")
    }
    function farRows() {
        fresh(); appendRows(120, "m", 30)
        layout.positionAtRow(40, false); settle()
        const top = flick.contentY, bottom = top + 240
        let far = 0
        for (let i = 0; i < layout.count; ++i) {
            if (!layout.itemAt(i)) continue
            const rowTop = layout.positionOf(i), rowBottom = rowTop + layout.heightOf(i)
            if (rowBottom < top - 480 || rowTop > bottom + 480) far++
        }
        check(far === 0, "no row created far from the viewport")
        check(!layout.itemAt(0) && !layout.itemAt(119), "first and last rows not created")
    }
    function contiguous() {
        fresh(); appendRows(50, "m", 30)
        layout.positionAtRow(20, false); settle()
        let previous = -1, ok = true, created = 0
        for (let i = 0; i < layout.count; ++i) {
            const item = layout.itemAt(i)
            if (!item) continue
            created++
            if (Math.abs(layout.heightOf(i) - item.height) > 0.5) { ok = false; console.log("height", i) }
            if (Math.abs(item.y - layout.positionOf(i)) > 0.5) { ok = false; console.log("y", i) }
            if (item.index !== i || item.messageId !== "m" + i) { ok = false; console.log("roles", i, item.index, item.messageId) }
            if (previous >= 0 && Math.abs(layout.positionOf(i) - layout.positionOf(previous) - layout.heightOf(previous)) > 0.5) ok = false
            previous = i
        }
        check(ok && created > 0, "created rows match delegate heights, get model and index, and are contiguous (" + created + ")")
    }

    Timer {
        interval: 50; running: true
        onTriggered: {
            try {
                following(); anchor(); reset(); widthChange(); neighbours(); farRows(); contiguous()
            } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack) }
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
