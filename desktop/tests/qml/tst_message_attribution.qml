import QtQuick
import QtTest
import "../../qml/components"

// A reply to another agent is the current agent's own message. Only an
// incoming prompt keeps the other agent's name in the text-only chat.
TestCase {
    id: testCase
    name: "MessageAttribution"
    width: 640; height: 400; visible: true
    when: windowShown

    QtObject {
        id: stub
        property int agentRevision: 0
        property int avatarRevision: 0
        property int mediaRevision: 0
        property bool toolsVisible: false
        property bool timestampsVisible: false
        property bool sharedFilesystem: false
        property var toolNarrator: null
        function agentSessionById(agentId) { return agentId === "agent-cpp" ? "cjunior-0940" : ""; }
        function agentWorkingDirectory(session) { return ""; }
        function avatarSource(session) { return ""; }
        function resolveMediaMarkdown(text) { return text; }
        function markdownDisplayBlocks(text) { return [text]; }
        function loadMessageToolDetails(session, id) {}
    }

    Component {
        id: factory
        MessageDelegate {
            controller: stub
            session: "hugo"
            messageId: "m-1"
            timestamp: ""
            messageKind: "final_answer"
            toolName: ""
            senderName: ""
            pending: false
            deliveryFailed: false
            activity: false
            activityStatus: ""
            automated: false
            category: ""
            tools: []
            displayCells: []
            activityCount: 0
            toolDetailsAvailable: false
            showTools: false
            showTimestamp: false
            width: testCase.width
        }
    }

    function test_incomingAgentPromptKeepsItsSenderName() {
        const row = createTemporaryObject(factory, testCase, {
            authorRole: "user", origin: "agent", body: "Status: survey done",
            senderName: "C++ Junior", senderAgentId: "agent-cpp", delivery: "sent"});
        verify(row.teamAuthored);
        verify(row.rightAligned);
        const avatar = findChild(row, "teamMessageAvatar");
        verify(avatar === null || !avatar.visible);
        compare(findChild(row, "groupAuthorName").text, "C++ Junior");
        verify(findChild(row, "groupAuthorLine").visible);
        const marker = findChild(row, "replyMarker");
        verify(marker === null || !marker.visible);
    }

    function test_replyIsAuthoredLocallyAndOnlyMarksTheAnsweredAgent() {
        const row = createTemporaryObject(factory, testCase, {
            authorRole: "assistant", origin: "agent",
            body: "Good, that matches the agreed scope.",
            replyToAgentId: "agent-cpp", replyToName: "C++ Junior",
            replyToSession: "cjunior-0940", delivery: "private"});
        // The regression: an agent-origin assistant row must not be attributed
        // to the agent it answers.
        verify(!row.teamAuthored);
        verify(!row.userAuthored);
        const avatar = findChild(row, "teamMessageAvatar");
        verify(avatar === null || !avatar.visible);
        const marker = findChild(row, "replyMarker");
        verify(marker !== null && marker.visible);
        verify(marker.text.indexOf("Replying to C++ Junior") >= 0);
        verify(marker.text.indexOf("private reply") >= 0);
        // The marker never duplicates the quoted text of the answered message.
        verify(marker.text.indexOf("Status: survey done") < 0);
    }

    function test_ordinaryUserAndAssistantRowsAreUnchanged() {
        const mine = createTemporaryObject(factory, testCase, {
            authorRole: "user", origin: "user", body: "Peter asks something"});
        verify(mine.userAuthored && !mine.teamAuthored && mine.rightAligned);
        const plain = createTemporaryObject(factory, testCase, {
            authorRole: "assistant", origin: "user", body: "An ordinary answer"});
        verify(!plain.teamAuthored && !plain.userAuthored && !plain.rightAligned);
        const marker = findChild(plain, "replyMarker");
        verify(marker === null || !marker.visible);
    }

    function test_pairRoomShowsBothAuthorNamesAndReplyMarkers() {
        const incoming = createTemporaryObject(factory, testCase, {
            groupView: true, authorRole: "user", origin: "agent",
            body: "Status: survey done", senderName: "C++ Junior",
            senderAgentId: "agent-cpp", delivery: "sent"});
        verify(!incoming.rightAligned);
        const incomingAvatar = findChild(incoming, "teamMessageAvatar");
        verify(incomingAvatar === null || !incomingAvatar.visible);
        compare(findChild(incoming, "groupAuthorName").text, "C++ Junior");
        verify(!findChild(incoming, "groupReplyMarker").visible);

        const answer = createTemporaryObject(factory, testCase, {
            groupView: true, authorRole: "assistant", origin: "agent",
            body: "Good, that matches.", senderName: "Hugo", senderAgentId: "agent-hugo",
            replyToAgentId: "agent-cpp", replyToName: "C++ Junior", delivery: "private"});
        compare(findChild(answer, "groupAuthorName").text, "Hugo");
        const groupMarker = findChild(answer, "groupReplyMarker");
        verify(groupMarker.visible);
        verify(groupMarker.text.indexOf("Replying to C++ Junior") >= 0);
        // In the room view the inline marker is not duplicated.
        const inline = findChild(answer, "replyMarker");
        verify(inline === null || !inline.visible);
    }
}
