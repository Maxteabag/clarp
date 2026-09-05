import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "ActivityLayout"
    when: windowShown
    width: 520
    height: 420
    visible: true

    QtObject {
        id: stubController
        property int mediaRevision: 0
        function resolveMediaMarkdown(text) { return text; }
        function markdownDisplayBlocks(text) { return text.split("\n\n"); }
    }

    Component {
        id: toolOnlyMessage
        Clarp.MessageDelegate {
            controller: stubController
            session: "fixture"
            messageId: "tool-only"
            authorRole: "assistant"
            body: ""
            timestamp: ""
            messageKind: ""
            toolName: ""
            origin: ""
            senderName: ""
            pending: false
            deliveryFailed: false
            activity: false
            activityStatus: ""
            automated: false
            category: ""
            tools: []
            displayCells: [{title: "Read", summary: "MessageDelegate.qml", status: "ok"}]
            activityCount: 1
            toolDetailsAvailable: false
            showTools: true
            showTimestamp: false
        }
    }

    function test_toolOnlyMessageHasNoPhantomBodySpacing() {
        const message = createTemporaryObject(toolOnlyMessage, testCase);
        verify(message !== null);
        waitForRendering(message);
        const card = findChild(message, "displayCellCard");
        verify(card !== null);
        const top = card.mapToItem(message, 0, 0).y;
        verify(top <= 1, "Empty message body must not reserve space before a tool");
        verify(message.implicitHeight - (top + card.height) <= 4,
               "Tool-only messages must not add a paragraph-sized trailing gap");
    }

    Component {
        id: toolCard
        Clarp.ToolCard {
            width: 320
            tool: ({name: "Bash", summary: "Build preview", command: "cmake --build desktop/build/dev"})
        }
    }

    function test_expandedToolGrowsToContainWrappedDetails() {
        const card = createTemporaryObject(toolCard, testCase);
        waitForRendering(card);
        const collapsedHeight = card.implicitHeight;
        card.expanded = true;
        tryVerify(() => card.implicitHeight > collapsedHeight);
        const expandedHeight = card.implicitHeight;
        card.width = 150;
        tryVerify(() => card.implicitHeight > expandedHeight);
    }

    QtObject {
        id: structuredCell
        property string title: "Edit"
        property string summary: "Compact tool rows"
        property list<var> lines: [{kind: "diff_new", text: "+ spacing: 1"}]
    }

    Component {
        id: displayCard
        Clarp.DisplayCellCard {
            width: 320
            cell: structuredCell
        }
    }

    function test_nativeSequenceDetailsRemainVisible() {
        const card = createTemporaryObject(displayCard, testCase);
        waitForRendering(card);
        const collapsedHeight = card.implicitHeight;
        card.expanded = true;
        tryVerify(() => card.implicitHeight > collapsedHeight,
                  1000, "Qt list properties must render the same details as JavaScript arrays");
    }
}
