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
        readIndex = index;
        readOffset = anchorOffset;
    }
    // The last row the reader had in view. Unlike the anchor it survives a
    // moment with no row in view, so a blank view can return to it.
    property int readIndex: -1
    property real readOffset: 0
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
            // ListView re-estimating can shift the rows just after a follow
            // landed, dropping the last row out of the loaded set with the
            // count unchanged. A few full follows a second bring it back; the
            // cap keeps estimate churn from driving a loop.
            if (count === followedCount) {
                if (followRescues.count >= 3) return;
                if (followRescues.count++ === 0) followRescues.start();
                // Before this frame paints: the last row is created again at
                // the end, then the view settles on its real bottom.
                positionViewAtEnd();
                settleAtEnd();
            }
            followedCount = count;
            scheduleDeferredFollow();
            return;
        }
        followedCount = count;
        if (Math.abs(contentY - endContentY(last)) > bottomMargin + 2) settleAtEnd();
    }
    Timer {
        id: followRescues
        property int count: 0
        interval: 1000
        onTriggered: count = 0
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
        readIndex = -1;
        scrollEpoch++;
        cancelFlick();
        wheelSettle.stop();
        userInteracting = false;
        followLatest = true;
        newMessagesBelow = false;
    }
    function beforeModelReset() {
        anchorIndex = -1;
        readIndex = -1;
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
        // Rows are contiguous, so a viewport showing none has passed an edge.
        // When the view lands wholly above the first row, ListView never
        // creates that row and the clamp above never applies: the transcript
        // stayed blank until the chat was switched.
        if (showsNoRow()) returnToRows(delta < 0);
    }
    function recoverIfBlank() {
        if (showsNoRow() && !moving && !userInteracting && !scrollBar.pressed) returnToRows(contentY < originY);
    }
    function showsNoRow() {
        return count > 0 && indexAt(width / 2, contentY + height / 2) < 0
            && indexAt(width / 2, contentY + 1) < 0 && indexAt(width / 2, contentY + height - 1) < 0;
    }
    // Goes back to the nearest row, not straight to an end: during a fast
    // scroll through rows of very different heights a viewport can briefly
    // hold no created row while rows beyond it are loaded, and jumping to
    // the end then skipped a thousand messages in one frame. Only when the
    // nearest created row is the first (or last) one has the edge been passed.
    function returnToRows(towardsBeginning) {
        let first = -1;
        let last = -1;
        for (let i = 0; i < count; ++i) {
            if (!itemAtIndex(i)) continue;
            if (first < 0) first = i;
            last = i;
        }
        console.warn("transcript returned to its rows:", towardsBeginning ? "up" : "down", "contentY", contentY,
            "originY", originY, "count", count, "created", first, "-", last, "read", readIndex);
        // The last row the reader had in view is the best place to return
        // to: after overshooting an edge it is the row next to that edge, and
        // after older history is prepended above the reader (94 -> 1,192 rows
        // in one step) it moved with the insert. Rows that are still created
        // can be stale then, so they are only the fallback.
        if (!followLatest && readIndex >= 0 && readIndex < count) {
            const index = readIndex;
            const offset = readOffset;
            anchoring = true;
            positionViewAtIndex(index, ListView.Beginning);
            forceLayout();
            const item = itemAtIndex(index);
            if (item) contentY = item.y - offset;
            anchoring = false;
            return;
        }
        if (towardsBeginning) {
            if (first > 0) {
                positionViewAtIndex(first - 1, ListView.End);
            } else {
                positionViewAtBeginning();
                contentY = originY - topMargin;
            }
        } else if (last >= 0 && last < count - 1) {
            positionViewAtIndex(last + 1, ListView.Beginning);
        } else {
            positionViewAtEnd();
            forceLayout();
            settleAtEnd();
        }
    }
    // A transcript with rows but none on screen was seen twice in an E2E run
    // (after a resize, and a wheel burst that landed at the top) and could
    // not be reproduced. Log the geometry once per episode so the next one,
    // in a real session, says where the view was.
    property bool blankReported: false
    Timer {
        interval: 1000
        repeat: true
        running: root.visible && root.count > 0
        onTriggered: {
            const blank = root.showsNoRow();
            if (blank && !root.blankReported)
                console.warn("transcript blank: contentY", root.contentY, "originY", root.originY,
                    "contentHeight", root.contentHeight, "height", root.height, "count", root.count,
                    "follow", root.followLatest, "anchor", root.anchorIndex, root.anchorOffset);
            root.blankReported = blank;
            // Whatever left it there, a blank view that is not being moved
            // returns to the nearest end of the rows.
            if (blank) root.recoverIfBlank();
        }
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
    // A narrower view wraps every row taller (a new split pane is laid out
    // once, then narrowed). The last row can leave the loaded set, and with it
    // the only follow path that a height estimate may not drive.
    onWidthChanged: scheduleFollow()
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
            if (root.readIndex >= first) root.readIndex += last - first + 1;
            // A page of older history can leave the view with no row; return
            // to the reader's message now rather than at the next watchdog tick.
            Qt.callLater(root.recoverIfBlank);
        }
        function onRowsRemoved(parent, first, last) {
            if (root.anchorIndex > last) root.anchorIndex -= last - first + 1;
            else if (root.anchorIndex >= first) root.anchorIndex = -1;
            if (root.readIndex > last) root.readIndex -= last - first + 1;
            else if (root.readIndex >= first) root.readIndex = -1;
        }
        function onModelReset() { root.afterModelReset(); }
        function onConversationIdChanged() { root.savedAnchor = null; root.scrollToLatest(); }
    }
}
