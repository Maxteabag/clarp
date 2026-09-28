import QtQuick
import QtQuick.Controls

// The conversation transcript: Clarp's own virtualized list (see
// docs/transcript-view.md). TranscriptLayout places rows from heights it
// knows and keeps the viewport on its anchor or on the end; this item owns
// the input (wheel, keys, scrollbar, flicks) and the follow intent. It keeps
// the API the pane and the smoke checks used on TranscriptList.
Item {
    id: root
    objectName: "transcriptList"

    property alias model: layout.model
    property alias delegate: layout.delegate
    property alias sectionDelegate: layout.sectionDelegate
    property alias sectionProperty: layout.sectionRole
    property Component header: null
    property Component footer: null
    property alias spacing: layout.spacing
    property alias leftMargin: layout.leftMargin
    property alias rightMargin: layout.rightMargin
    property alias topMargin: layout.topMargin
    property alias bottomMargin: layout.bottomMargin

    property alias followLatest: layout.following
    property bool newMessagesBelow: false
    property bool userInteracting: false
    property int scrollEpoch: 0

    // Flickable geometry, for the pane and the smoke checks.
    property alias contentY: flick.contentY
    readonly property alias contentHeight: flick.contentHeight
    readonly property real originY: 0
    readonly property alias moving: flick.moving
    readonly property int count: layout.count
    readonly property real distanceFromBottom: Math.max(0, flick.contentHeight - flick.height - flick.contentY)
    readonly property bool atYEnd: distanceFromBottom < 1
    readonly property bool atLatest: atYEnd

    function itemAtIndex(index) { return layout.itemAt(index); }
    function indexAt(x, y) {
        if (layout.count === 0 || y < layout.positionOf(0) || y >= layout.positionOf(layout.count)) return -1;
        return layout.rowAt(y);
    }
    function positionViewAtIndex(index, mode) { layout.positionAtRow(index, mode === ListView.End); }
    function positionViewAtBeginning() { stopFollowing("beginning"); flick.contentY = 0; layout.anchorToViewport(); }
    function positionViewAtEnd() { scrollToLatest(); }
    function forceLayout() { layout.layoutNow(); }
    function cancelFlick() { flick.cancelFlick(); }

    // Kept without parameters: C++ checks call it by name.
    function pauseFollowing() { stopFollowing("request"); }
    function stopFollowing(reason) {
        scrollEpoch++;
        if (followLatest)
            console.info("transcript stopped following:", reason, "distance", Math.round(distanceFromBottom), "count", count);
        followLatest = false;
    }
    function beginUserScroll(reason) {
        stopFollowing(reason || "user scroll");
        userInteracting = true;
    }
    function endUserScroll() {
        if (flick.moving || scrollBar.pressed || wheelSettle.running) return;
        userInteracting = false;
        // Only arriving at the end by the reader's own scrolling resumes
        // following; content arriving near the viewport does not.
        if (atLatest) {
            followLatest = true;
            newMessagesBelow = false;
        }
    }
    function scrollToLatest() {
        scrollEpoch++;
        flick.cancelFlick();
        wheelSettle.stop();
        userInteracting = false;
        followLatest = true;
        newMessagesBelow = false;
        layout.layoutNow();
    }
    // A different conversation is about to be bound.
    function resetForConversation() {
        scrollEpoch++;
        flick.cancelFlick();
        wheelSettle.stop();
        userInteracting = false;
        followLatest = true;
        newMessagesBelow = false;
    }
    // The layout keeps the reader on the same message across resets itself.
    function beforeModelReset() {}
    function afterModelReset() {}

    function scrollBy(delta) {
        flick.contentY = Math.max(0, Math.min(layout.endY(), flick.contentY + delta));
    }
    function handleScrollKey(event) {
        if (![Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End].includes(event.key)
            || (event.modifiers & (Qt.AltModifier | Qt.MetaModifier))) { event.accepted = false; return; }
        if (event.key === Qt.Key_End) {
            scrollToLatest();
        } else {
            beginUserScroll("key");
            if (event.key === Qt.Key_Home) flick.contentY = 0;
            else scrollBy(event.key === Qt.Key_Up ? -40 : event.key === Qt.Key_Down ? 40
                : event.key === Qt.Key_PageUp ? -flick.height * 0.9 : flick.height * 0.9);
            wheelSettle.restart();
        }
        event.accepted = true;
    }
    Keys.onPressed: event => handleScrollKey(event)

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: layout.height
        boundsBehavior: Flickable.StopAtBounds
        onMovementStarted: root.beginUserScroll("flick")
        onMovementEnded: root.endUserScroll()

        TranscriptLayout {
            id: layout
            width: flick.width
            flickable: flick
            // While the reader scrolls, rows fill in over a few frames rather
            // than holding the scroll; opening a chat creates them at once.
            creationBudget: root.userInteracting ? 10 : 0
            header: headerLoader.item ? headerLoader : null
            footer: footerLoader.item ? footerLoader : null
        }

            // On the Flickable: handlers run before its own wheel scrolling.
        WheelHandler {
            target: null
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: event => {
                const delta = event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y / 120 * 60;
                if (delta === 0) { event.accepted = false; return; }
                flick.cancelFlick();
                root.beginUserScroll("wheel");
                root.scrollBy(-delta);
                wheelSettle.restart();
                event.accepted = true;
            }
        }
        ScrollBar.vertical: ScrollBar {
            id: scrollBar
            objectName: "transcriptScrollBar"
            onPressedChanged: pressed ? root.beginUserScroll("scrollbar") : root.endUserScroll()
        }
    }
    Loader { id: headerLoader; sourceComponent: root.header; width: root.width }
    Loader { id: footerLoader; sourceComponent: root.footer; width: root.width }

    Timer { id: wheelSettle; interval: 150; onTriggered: root.endUserScroll() }
    // The layout keeps a following view on the end by construction. Check it
    // anyway: a following view above its end at rest is a bug worth logging
    // (the rig scenarios fail on this line), and it is corrected.
    Timer {
        interval: 1000
        repeat: true
        running: root.visible && root.followLatest && root.count > 0
        property bool seen: false
        onTriggered: {
            const drifted = !root.userInteracting && !flick.moving && root.distanceFromBottom > 2;
            if (drifted && seen) {
                console.warn("transcript drifted while following: distance", Math.round(root.distanceFromBottom),
                    "count", root.count, "measured", layout.measuredCount);
                layout.layoutNow();
            }
            seen = drifted;
        }
    }
}
