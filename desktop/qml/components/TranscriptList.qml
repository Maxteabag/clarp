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
        if (followLatest && !userInteracting && followTicket === scrollEpoch && visible)
            positionViewAtEnd();
    }
    function scheduleFollow() {
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
        scheduleFollow();
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
        if (followLatest) { scheduleFollow(); return; }
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
    onContentHeightChanged: scheduleFollow()
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
