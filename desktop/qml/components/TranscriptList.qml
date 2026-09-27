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
    // Measured to the same end that following settles on (endContentY): the
    // estimated extent can sit tens of pixels past the real last row, and a
    // reader who wheels back to that row would otherwise never count as
    // having reached the end, so following would not resume.
    readonly property real distanceFromBottom: Math.max(0,
        endContentY(count > 0 ? itemAtIndex(count - 1) : null) - contentY)
    readonly property bool atLatest: atYEnd || distanceFromBottom < 1

    function pauseFollowing() {
        scrollEpoch++;
        followLatest = false;
    }
    onFollowLatestChanged: if (followLatest) anchorIndex = -1
    function beginUserScroll() {
        pauseFollowing();
        userInteracting = true;
    }
    function endUserScroll() {
        if (moving || scrollBar.pressed || wheelSettle.running) return;
        userInteracting = false;
        // Only actual user arrival at the end resumes following. A layout
        // change or a message arriving near the viewport must not do so.
        if (atLatest) {
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
    // Scroll anchoring for a paused reader. After the view moves, ListView
    // creates the rows coming into view and, in its next layout pass, can lay
    // the loaded rows out again from its height estimates: every one of them
    // shifts by tens of pixels under an unchanged contentY, so the text jumps
    // and the real end moves away (wheeling back the same distance then stops
    // short of it). Keep the row that was mid-screen where the reader left it.
    // The target is a loaded row's own y, never an estimate, and corrections
    // are capped per event-loop turn, so this cannot chase itself.
    property int anchorIndex: -1
    property real anchorOffset: 0
    property int anchorCorrections: 0
    property bool anchoring: false
    // Every scroll moves the anchor with it (wheel, scrollbar, keys, touchpad,
    // jumps to a message); only the anchoring's own corrections do not.
    // Remembering it on the wheel alone snapped the view back to the last
    // wheel position after scrolling any other way.
    onContentYChanged: if (!anchoring && !followLatest) rememberAnchor()
    function rememberAnchor() {
        anchorIndex = -1;
        if (followLatest || count === 0) return;
        const index = indexAt(width / 2, contentY + height / 2);
        const item = index < 0 ? null : itemAtIndex(index);
        if (!item) return;
        anchorIndex = index;
        anchorOffset = item.y - contentY;
    }
    function keepAnchor() {
        if (followLatest || anchorIndex < 0 || anchorIndex >= count || scrollBar.pressed || moving) return;
        const item = itemAtIndex(anchorIndex);
        if (!item) return;
        const target = item.y - anchorOffset;
        if (Math.abs(contentY - target) <= 0.5 || anchorCorrections >= 3) return;
        if (anchorCorrections++ === 0) Qt.callLater(() => { root.anchorCorrections = 0; });
        anchoring = true;
        contentY = target;
        anchoring = false;
    }
    function followContentHeight() {
        if (!followLatest) Qt.callLater(root.keepAnchor);
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
        anchorIndex = -1;
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
    // Moves the view by a user scroll. The limits come from rows that exist,
    // not from ListView's estimates: while the rows above the viewport have
    // not been created (one very tall reply is enough), originY equals the
    // current position, so clamping to it turned a wheel-up into no movement.
    // Past an edge whose row is not created yet the view moves freely;
    // ListView creates the rows it reaches, and the result is clamped again
    // once they exist.
    function scrollBy(delta) {
        const clamp = () => {
            const first = count > 0 ? itemAtIndex(0) : null;
            const last = count > 0 ? itemAtIndex(count - 1) : null;
            const minimum = first ? originY - topMargin : -Infinity;
            const maximum = last ? Math.max(minimum, endContentY(last)) : Infinity;
            return {minimum, maximum};
        };
        let bounds = clamp();
        contentY = Math.max(bounds.minimum, Math.min(bounds.maximum, contentY + delta));
        forceLayout();
        bounds = clamp();
        if (contentY < bounds.minimum) contentY = bounds.minimum;
        else if (contentY > bounds.maximum) contentY = bounds.maximum;
    }
    function handleScrollKey(event) {
        if (![Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End].includes(event.key)
            || (event.modifiers & (Qt.AltModifier | Qt.MetaModifier))) { event.accepted = false; return; }
        if (event.key === Qt.Key_End) scrollToLatest();
        else {
            beginUserScroll();
            if (event.key === Qt.Key_Home) {
                positionViewAtBeginning();
                contentY = originY - topMargin;
            } else {
                scrollBy(event.key === Qt.Key_Up ? -40 : event.key === Qt.Key_Down ? 40
                    : event.key === Qt.Key_PageUp ? -height * 0.9 : height * 0.9);
            }
            wheelSettle.restart();
        }
        event.accepted = true;
    }
    onMovementStarted: beginUserScroll()
    onMovementEnded: { endUserScroll(); rememberAnchor(); }
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
            root.scrollBy(-delta);
            root.rememberAnchor();
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
        // Older history arrives above the reader; keep the anchor on its row.
        function onRowsInserted(parent, first, last) {
            if (root.anchorIndex >= first) root.anchorIndex += last - first + 1;
        }
        function onRowsRemoved(parent, first, last) {
            if (root.anchorIndex > last) root.anchorIndex -= last - first + 1;
            else if (root.anchorIndex >= first) root.anchorIndex = -1;
        }
        function onModelReset() { root.afterModelReset(); }
        function onConversationIdChanged() { root.savedAnchor = null; root.scrollToLatest(); }
    }
}
