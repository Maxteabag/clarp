import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "TranscriptScroll"
    when: windowShown
    visible: true
    width: 500
    height: 400
    ListModel { id: rows }
    Component {
        id: viewComponent
        Clarp.TranscriptList {
            width: testCase.width
            height: testCase.height
            model: rows
            clip: true
            delegate: Rectangle {
                required property string messageId
                required property int rowHeight
                width: ListView.view.width
                height: rowHeight
                color: "#202330"
                Text { text: parent.messageId }
            }
        }
    }
    function init() {
        rows.clear();
        for (let i = 0; i < 60; i++) rows.append({messageId: "m" + i, rowHeight: 48});
    }
    function openView() {
        const view = createTemporaryObject(viewComponent, testCase);
        verify(view !== null);
        tryVerify(() => view.atYEnd);
        return view;
    }
    function test_wheelCancelsQueuedAutoFollowAndStreamingCannotPullBack() {
        const view = openView();
        view.scrollToLatest(); // A deferred follow is already queued.
        const epoch = view.scrollEpoch;
        mouseWheel(view, 200, 200, 0, 480);
        verify(view.scrollEpoch > epoch, "The wheel event must reach the transcript handler");
        tryVerify(() => !view.atYEnd);
        wait(200);
        compare(view.followLatest, false);
        const position = view.contentY;
        rows.append({messageId: "new", rowHeight: 200});
        rows.setProperty(59, "rowHeight", 250);
        wait(100);
        verify(Math.abs(view.contentY - position) < 2, "Streaming must not move a reader back to the bottom");
    }
    function test_scrollbarDragTurnsOffFollowing() {
        const view = openView();
        const bar = findChild(view, "transcriptScrollBar");
        verify(bar !== null);
        mousePress(bar, bar.width / 2, bar.height - 20);
        mouseMove(bar, bar.width / 2, bar.height / 2, 40);
        mouseRelease(bar, bar.width / 2, bar.height / 2);
        wait(200);
        compare(view.followLatest, false);
        const position = view.contentY;
        rows.append({messageId: "new", rowHeight: 150});
        wait(100);
        verify(Math.abs(view.contentY - position) < 2);
    }
    function test_prependKeepsVisibleMessageAndOriginAwareDistance() {
        const view = openView();
        view.followLatest = false;
        view.positionViewAtIndex(25, ListView.Beginning);
        waitForRendering(view);
        const anchor = view.itemAtIndex(25);
        const offset = anchor.y - view.contentY;
        rows.insert(0, {messageId: "older", rowHeight: 160});
        wait(100);
        const same = view.itemAtIndex(26);
        verify(same !== null);
        compare(same.messageId, "m25");
        verify(Math.abs(same.y - view.contentY - offset) < 2);
        view.scrollToLatest();
        tryVerify(() => view.atYEnd);
        verify(view.distanceFromBottom < 2, "Virtualized origin must be included in end-distance calculation");
    }
    function test_keyboardScrollDoesNotJumpToTheListSelection() {
        const view = openView();
        view.forceActiveFocus();
        const before = view.contentY;
        keyClick(Qt.Key_Up);
        wait(200);
        compare(view.followLatest, false);
        verify(Math.abs(view.contentY - (before - 40)) < 2);
        keyClick(Qt.Key_End);
        tryVerify(() => view.atYEnd && view.followLatest);
    }
    function test_refreshRestoresMessageIdentityAndPixelOffset() {
        const view = openView();
        view.pauseFollowing();
        view.positionViewAtIndex(25, ListView.Beginning);
        view.contentY += 12;
        waitForRendering(view);
        const offset = view.itemAtIndex(25).y - view.contentY;
        view.beforeModelReset();
        rows.clear();
        rows.append({messageId: "earlier", rowHeight: 160});
        for (let i = 0; i < 60; ++i) rows.append({messageId: "m" + i, rowHeight: 48});
        view.afterModelReset();
        tryVerify(() => view.itemAtIndex(26) !== null);
        tryVerify(() => Math.abs(view.itemAtIndex(26).y - view.contentY - offset) < 2);
        compare(view.followLatest, false);
    }
    function test_refreshWithVanishedAnchorDoesNotResetToTop() {
        const view = openView();
        view.pauseFollowing();
        view.positionViewAtIndex(25, ListView.Beginning);
        waitForRendering(view);
        view.beforeModelReset();
        rows.clear();
        for (let i = 0; i < 60; ++i) rows.append({messageId: "replacement" + i, rowHeight: 48});
        view.afterModelReset();
        tryVerify(() => view.contentY > 500);
        compare(view.followLatest, false);
    }
}
