import QtQuick
import QtQuick.Controls

ListView {
    id: root
    objectName: "transcriptList"
    keyNavigationEnabled: false
    property bool followLatest: true
    property bool newMessagesBelow: false
    property bool userInteracting: false
    property int scrollEpoch: 0
    property int followTicket: 0
    property var savedAnchor: null
    readonly property real distanceFromBottom: Math.max(0,
        originY + contentHeight + bottomMargin - height - contentY)

    function pauseFollowing() {
        scrollEpoch++;
        followLatest = false;
    }
    function beginUserScroll() {
        pauseFollowing();
        userInteracting = true;
    }
    function endUserScroll() {
        if (moving || scrollBar.pressed || wheelSettle.running) return;
        userInteracting = false;
        // Only actual user arrival at the end resumes following. A layout
        // change or a message arriving near the viewport must not do so.
        if (atYEnd) {
            followLatest = true;
            newMessagesBelow = false;
        }
    }
    function applyFollow() {
        if (!followLatest || userInteracting || followTicket !== scrollEpoch || !visible) return;
        forceLayout();
        positionViewAtEnd();
        forceLayout();
        // positionViewAtEnd aligns the last item, but does not include the
        // trailing margin. Finish below the last item itself.
        settleAtEnd();
        followedCount = count;
    }
    // The bottom of the last delegate, never originY + contentHeight: those are
    // estimates from the average loaded row height. One very tall row (a wide
    // table) swings that estimate, and chasing it moves the viewport onto
    // unloaded rows, which reloads the tall row and swings it back. Inside a
    // layout pass that loop never ended: the GUI froze while regenerated
    // delegates piled up until the process was killed for memory.
    // An explicit follow (open, append, resize) may run before the last row is
    // created; then the estimate is all there is, and one jump to it is fine.
    // followContentHeight never takes that path, so estimates cannot loop.
    function endContentY(last) {
        // The footer sits directly below the last row; its own y can lag a
        // layout pass behind, so add its height to the row instead.
        const bottom = !last ? originY + contentHeight
            : last.y + last.height + (footerItem ? footerItem.height : 0);
        return Math.max(originY - topMargin, bottom + bottomMargin - height);
    }
    function settleAtEnd() {
        const target = endContentY(count > 0 ? itemAtIndex(count - 1) : null);
        if (Math.abs(contentY - target) > 0.5) contentY = target;
    }
    // Cheap, re-entrancy safe follow for the frame about to be rendered. A
    // deferred follow alone lets one frame paint at the old position first,
    // which reads as a flicker whenever the view grows, shrinks or resets.
    function followNow() {
        if (!followLatest || userInteracting || !visible) return;
        settleAtEnd();
    }
    function scheduleFollow() {
        if (!followLatest || userInteracting) return;
        followNow();
        followTicket = scrollEpoch;
        Qt.callLater(root.applyFollow);
    }
    // Row height estimates change contentHeight without any content moving.
    // Only a real change at the end (the last row growing while it streams)
    // should move the view; when the last row is already in place this is a
    // no-op, so estimate churn cannot drive scrolling.
    // Even a small contentY write restarts ListView's re-estimation. In that
    // churn the loaded rows move with the viewport and the gap below the last
    // row only flickers by the margin; real growth (a streamed line, a row
    // finishing layout) opens a larger gap. Answer only the larger gap, and
    // schedule a full follow only when rows were actually added.
    property int followedCount: -1
    function followContentHeight() {
        if (!followLatest || userInteracting || !visible) return;
        const last = count > 0 ? itemAtIndex(count - 1) : null;
        if (!last) {
            if (count === followedCount) return;
            followedCount = count;
            scheduleDeferredFollow();
            return;
        }
        followedCount = count;
        if (Math.abs(contentY - endContentY(last)) > bottomMargin + 2) settleAtEnd();
    }
    function scheduleDeferredFollow() {
        if (!followLatest || userInteracting) return;
        followTicket = scrollEpoch;
        Qt.callLater(root.applyFollow);
    }
    function scrollToLatest() {
        scrollEpoch++;
        cancelFlick();
        wheelSettle.stop();
        userInteracting = false;
        followLatest = true;
        newMessagesBelow = false;
        followTicket = scrollEpoch;
        if (visible && count > 0) applyFollow();
        scheduleFollow();
    }
    // A different conversation is about to be bound: its position is unrelated
    // to the anchor or pause state of the one being replaced.
    function resetForConversation() {
        savedAnchor = null;
        scrollEpoch++;
        cancelFlick();
        wheelSettle.stop();
        userInteracting = false;
        followLatest = true;
        newMessagesBelow = false;
    }
    function beforeModelReset() {
        savedAnchor = null;
        if (followLatest || count === 0) return;
        let index = -1;
        for (let y = 1; y < height && index < 0; y += 8)
            index = indexAt(width / 2, contentY + y);
        const item = index < 0 ? null : itemAtIndex(index);
        if (item) savedAnchor = {id: String(item.messageId || ""), index: index,
            offset: item.y - contentY, logicalY: contentY - originY, epoch: scrollEpoch};
    }
    function afterModelReset() {
        const anchor = savedAnchor;
        savedAnchor = null;
        if (followLatest) {
            // The view regenerated at the top; land at the end before the
            // next frame instead of leaving that frame visible.
            followTicket = scrollEpoch;
            if (visible && count > 0) applyFollow();
            scheduleFollow();
            return;
        }
        if (!anchor) return;
        Qt.callLater(() => {
            if (anchor.epoch !== root.scrollEpoch || root.followLatest || root.userInteracting) return;
            let index = -1;
            if (root.model && typeof root.model.indexOfMessage === "function")
                index = root.model.indexOfMessage(anchor.id);
            else if (root.model && typeof root.model.get === "function") {
                for (let i = 0; i < root.count; ++i)
                    if (String(root.model.get(i).messageId) === anchor.id) { index = i; break; }
            }
            if (index < 0) {
                const minimum = root.originY - root.topMargin;
                const maximum = Math.max(minimum, root.originY + root.contentHeight + root.bottomMargin - root.height);
                root.contentY = Math.max(minimum, Math.min(maximum, root.originY + anchor.logicalY));
                return;
            }
            root.positionViewAtIndex(index, ListView.Beginning);
            root.forceLayout();
            const item = root.itemAtIndex(index);
            if (item) root.contentY = item.y - anchor.offset;
        });
    }
    function handleScrollKey(event) {
        if (![Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End].includes(event.key)
            || (event.modifiers & (Qt.AltModifier | Qt.MetaModifier))) { event.accepted = false; return; }
        if (event.key === Qt.Key_End) scrollToLatest();
        else {
            beginUserScroll();
            const minimum = originY - topMargin;
            const maximum = Math.max(minimum, originY + contentHeight + bottomMargin - height);
            const delta = event.key === Qt.Key_Up ? -40 : event.key === Qt.Key_Down ? 40
                : event.key === Qt.Key_PageUp ? -height * 0.9 : height * 0.9;
            contentY = event.key === Qt.Key_Home ? minimum : Math.max(minimum, Math.min(maximum, contentY + delta));
            wheelSettle.restart();
        }
        event.accepted = true;
    }
    onMovementStarted: beginUserScroll()
    onMovementEnded: endUserScroll()
    onContentHeightChanged: followContentHeight()
    onHeightChanged: scheduleFollow()
    onVisibleChanged: { if (visible) scheduleFollow(); }
    onModelChanged: { savedAnchor = null; scrollToLatest(); }
    Component.onCompleted: scrollToLatest()

    WheelHandler {
        target: null
        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
        onWheel: event => {
            const delta = event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y / 120 * 60;
            if (delta === 0) { event.accepted = false; return; }
            root.cancelFlick();
            root.beginUserScroll();
            const minimum = root.originY - root.topMargin;
            const maximum = Math.max(minimum, root.originY + root.contentHeight + root.bottomMargin - root.height);
            root.contentY = Math.max(minimum, Math.min(maximum, root.contentY - delta));
            wheelSettle.restart();
            event.accepted = true;
        }
    }
    Timer { id: wheelSettle; interval: 150; onTriggered: root.endUserScroll() }
    Keys.onPressed: event => root.handleScrollKey(event)
    ScrollBar.vertical: ScrollBar {
        id: scrollBar
        objectName: "transcriptScrollBar"
        onPressedChanged: pressed ? root.beginUserScroll() : root.endUserScroll()
        Keys.onPressed: event => root.handleScrollKey(event)
    }
    Connections {
        target: root.model
        ignoreUnknownSignals: true
        function onModelAboutToBeReset() { root.beforeModelReset(); }
        function onModelReset() { root.afterModelReset(); }
        function onConversationIdChanged() { root.savedAnchor = null; root.scrollToLatest(); }
    }
}
