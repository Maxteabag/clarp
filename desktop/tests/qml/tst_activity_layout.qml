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
        property var toolNarrator: null
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

    function test_userMessagesHaveBackgroundInsteadOfAnAccentLine() {
        const message = createTemporaryObject(toolOnlyMessage, testCase, {
            authorRole: "user", body: "Keep my messages easy to distinguish.",
            displayCells: [], activityCount: 0
        });
        waitForRendering(message);
        const background = findChild(message, "userMessageBackground");
        verify(background !== null);
        compare(background.color, "#493651");
        verify(background.x > 0, "Outgoing bubbles align right in the combined redesign");
        for (const child of background.children)
            verify(!(child.visible && child.width === 2), "User messages must not retain the left accent line");
    }

    QtObject {
        id: narratorStub
        property bool enabled: true
        property int revision: 0
        property bool ready: false
        property bool unavailable: false
        property string responseText: "Build the desktop preview."
        signal changed()
        function request(activity) {}
        function explanation(activity) { return ready ? responseText : ""; }
    }

    function test_translationIsBlueOptionalAndKeepsRawDetails() {
        narratorStub.enabled = true;
        narratorStub.ready = false;
        const card = createTemporaryObject(toolCard, testCase, {narrator: narratorStub});
        waitForRendering(card);
        const explanation = findChild(card, "activityExplanationText");
        verify(explanation !== null);
        compare(explanation.visible, true);
        compare(explanation.text, "Explaining activity…");
        narratorStub.ready = true;
        narratorStub.revision++;
        tryCompare(explanation, "visible", true);
        compare(explanation.text, "Build the desktop preview.");
        compare(explanation.color, "#82aaff");
        card.expanded = true;
        verify(card.detail.includes("cmake --build"));
        narratorStub.ready = false;
        narratorStub.unavailable = true;
        narratorStub.revision++;
        tryCompare(explanation, "text", "Explanation unavailable");
        verify(!visibleText(card).includes("cmake --build"));
        narratorStub.enabled = false;
        tryCompare(explanation, "visible", false);
    }

    function visibleText(item) {
        if (!item.visible) return "";
        let result = item.text !== undefined ? String(item.text) : "";
        for (const child of item.children || []) result += " " + visibleText(child);
        return result;
    }

    function cleanup() {
        stubController.toolNarrator = null;
        narratorStub.responseText = "Build the desktop preview.";
        narratorStub.unavailable = false;
    }

    function test_liveActivityNeverLeaksRawCommandsWhileWaiting() {
        narratorStub.enabled = true;
        narratorStub.ready = false;
        stubController.toolNarrator = narratorStub;
        const message = createTemporaryObject(toolOnlyMessage, testCase, {
            activity: true, activityStatus: "running", toolName: "node private_script.js",
            body: "Using node private_script.js --raw-detail", displayCells: [], activityCount: 0
        });
        waitForRendering(message);
        verify(!visibleText(message).includes("private_script.js"));
        verify(visibleText(message).includes("Explaining activity…"));
        message.width = 170;
        narratorStub.responseText = "Search the grocery catalogue for meat and compare prices per kilogram, sorted from cheapest to most expensive.";
        narratorStub.ready = true;
        narratorStub.revision++;
        tryVerify(() => message.implicitHeight > 40,
            1000, "Long human explanations must grow the live row instead of overlapping the next item");
    }
}
