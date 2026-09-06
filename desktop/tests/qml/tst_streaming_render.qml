import QtQuick
import QtTest
import QtQuick.Controls
import "../../qml/components" as Clarp
TestCase {
    id: testCase
    name: "StreamingRender"
    width: 600; height: 600; visible: true
    when: windowShown
    QtObject {
        id: stub
        property int mediaRevision: 0
        property var toolNarrator: null
        property bool sharedFilesystem: false
        function resolveMediaMarkdown(text) { return text; }
        function markdownDisplayBlocks(text) { return text.split("\n\n"); }
    }
    Component {
        id: messageComponent
        Clarp.MessageDelegate {
            controller: stub; session: "fixture"; messageId: "stream"; authorRole: "assistant"
            body: "Beginning"; timestamp: ""; messageKind: "live"; toolName: ""; origin: ""
            senderName: ""; pending: false; deliveryFailed: false; activity: false; activityStatus: ""
            automated: false; category: ""; tools: []; displayCells: []; activityCount: 0
            toolDetailsAvailable: false; showTools: false; showTimestamp: false
        }
    }
    SignalSpy { id: additions; signalName: "itemAdded" }
    function test_streamRetainsTextEditor() {
        const message = createTemporaryObject(messageComponent, testCase);
        verify(message !== null);
        const repeater = findChild(message, "messageBlockRepeater");
        additions.target = repeater;
        additions.clear();
        const editor = repeater.itemAt(0);
        for (let i = 0; i < 80; ++i) {
            message.body += " streamed content";
            wait(1);
        }
        console.log("Text editors recreated during 80 streamed updates:", additions.count);
        compare(additions.count, 0);
        compare(repeater.itemAt(0), editor);
        compare(editor.text, message.renderedBody);
        message.messageKind = "message";
        tryCompare(editor, "textFormat", TextEdit.MarkdownText);
        compare(repeater.itemAt(0), editor);
    }
}
