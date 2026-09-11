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
        property int avatarRevision: 0
        property int agentRevision: 0
        function avatarSource(session) { return ""; }
        function agentSessionById(id) { return id === "sender-id" ? "sender-now" : ""; }
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
    function test_collapsedGroupDoesNotInstantiateExplanationCards() {
        const message = createTemporaryObject(toolOnlyMessage, testCase, {
            showTools: false, groupSummary: "5 tool calls", groupedExpanded: false
        });
        verify(message !== null);
        waitForRendering(message);
        compare(findChild(message, "displayCellCard"), null);
        message.groupedExpanded = true;
        tryVerify(() => findChild(message, "displayCellCard") !== null);
        message.groupedExpanded = false;
        tryVerify(() => findChild(message, "displayCellCard") === null);
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
        compare(background.color, "#20212e");
        compare(background.radius, 0);
        verify(background.x > 0, "Outgoing bubbles align right in the combined redesign");
        for (const child of background.children)
            verify(!(child.visible && child.width === 2), "User messages must not retain the left accent line");
    }

    function test_agentMessageKeepsRightAlignmentAndSenderName() {
        const message = createTemporaryObject(toolOnlyMessage, testCase, {
            authorRole: "user", origin: "agent", senderName: "C++ Agent",
            senderAgentId: "sender-id", senderSession: "sender-before",
            body: "The combined client is tested.", displayCells: [], activityCount: 0
        });
        verify(message !== null);
        waitForRendering(message);
        const bubble = findChild(message, "userMessageBackground");
        verify(bubble.x > 0);
        compare(bubble.radius, 0);
        const avatar = findChild(message, "teamMessageAvatar");
        verify(avatar === null || !avatar.visible);
        compare(findChild(message, "groupAuthorName").text, "C++ Agent");
        verify(findChild(message, "groupAuthorLine").visible);
        compare(findChild(message, "messageProvenance").visible, false);
    }

    function test_attachedToolSummaryShowsElapsedTimeWithoutLoadingCards() {
        const message = createTemporaryObject(toolOnlyMessage, testCase, {
            body: "Working on the fix.", showTools: false,
            activityCount: 21, activitySummary: "21 tool calls · 1m 23s elapsed"
        });
        verify(message !== null);
        waitForRendering(message);
        compare(findChild(message, "activitySummaryText").text, "Show · 21 tool calls · 1m 23s elapsed");
        compare(findChild(message, "displayCellCard"), null);
    }

    QtObject {
        id: narratorStub
        property bool enabled: true
        property int revision: 0
        property bool ready: false
        property bool unavailable: false
        property string responseText: "Build the desktop preview."
        property bool rowFailed: false
        signal changed()
        function request(activity) {}
        function explanation(activity) { return ready ? responseText : ""; }
        function failed(activity) { return rowFailed; }
    }

    function test_translationIsBlueOptionalAndKeepsRawDetails() {
        narratorStub.enabled = true;
        narratorStub.ready = false;
        const card = createTemporaryObject(toolCard, testCase, {narrator: narratorStub});
        waitForRendering(card);
        const explanation = findChild(card, "activityExplanationText");
        verify(explanation !== null);
        compare(explanation.visible, true);
        verify([".", "..", "…"].includes(explanation.text));
        narratorStub.ready = true;
        narratorStub.revision++;
        tryCompare(explanation, "visible", true);
        compare(explanation.text, "Build the desktop preview.");
        compare(explanation.color, "#82aaff");
        card.expanded = true;
        verify(card.detail.includes("cmake --build"));
        // A translation that cannot arrive falls back to the tool call it was
        // replacing, rather than leaving a dead-end message in its place.
        narratorStub.ready = false;
        narratorStub.unavailable = true;
        narratorStub.revision++;
        tryCompare(explanation, "visible", false);
        verify(visibleText(card).includes("cmake --build"));
        verify(visibleText(card).includes("Bash"));
        narratorStub.unavailable = false;
        narratorStub.rowFailed = true;
        narratorStub.revision++;
        tryCompare(explanation, "visible", false);
        verify(visibleText(card).includes("cmake --build"));
        narratorStub.rowFailed = false;
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
        narratorStub.rowFailed = false;
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
        verify([".", "..", "…"].some(dots => visibleText(message).includes(dots)));
        message.width = 170;
        narratorStub.responseText = "Search the grocery catalogue for meat and compare prices per kilogram, sorted from cheapest to most expensive.";
        narratorStub.ready = true;
        narratorStub.revision++;
        tryVerify(() => message.implicitHeight > 40,
            1000, "Long human explanations must grow the live row instead of overlapping the next item");
    }
}
