import QtQuick
import QtTest
import "../../qml/components"

// The sidebar row for an agent pair: both avatars, an unread dot, and a
// selection that opens the projection instead of an agent.
TestCase {
    id: testCase
    name: "PairConversationRows"
    width: 360; height: 240; visible: true
    when: windowShown

    property var selected: []

    QtObject {
        id: stub
        property int avatarRevision: 0
        property string selectedSession: "hugo"
        function avatarSource(session) { return ""; }
        function chatStamp(value) { return value > 0 ? "12:04" : ""; }
        function selectSession(session) {
            testCase.selected.push(session);
            stub.selectedSession = session;
        }
        function isPairSession(session) { return String(session).indexOf("pair:") === 0; }
    }

    readonly property var room: ({
        conversation_id: "pair:agent-cpp:agent-hugo",
        title: "C++ Junior & Hugo",
        latest_activity: 1788750466681,
        latest_revision: 7,
        unread: true,
        participants: [{agent_id: "agent-cpp", session: "cjunior-0940", name: "C++ Junior"},
                       {agent_id: "agent-hugo", session: "hugo", name: "Hugo"}],
        latest_message: {text: "Good, that matches.", sender_name: "Hugo", delivery: "private"}
    })

    Component {
        id: factory
        PairRow { controller: stub; width: testCase.width }
    }

    function init() { testCase.selected = []; stub.selectedSession = "hugo"; }

    function test_rowShowsTitlePreviewAndUnread() {
        const row = createTemporaryObject(factory, testCase, {room: testCase.room});
        compare(findChild(row, "pairTitle").text, "C++ Junior & Hugo");
        compare(findChild(row, "pairPreview").text, "Hugo: Good, that matches.");
        verify(findChild(row, "pairUnreadDot").visible);
        verify(!row.current);
    }

    function test_selectionOpensTheProjectionAndClearsHighlight() {
        const row = createTemporaryObject(factory, testCase, {room: testCase.room});
        row.clicked();
        compare(testCase.selected, ["pair:agent-cpp:agent-hugo"]);
        verify(row.current);
    }

    function test_readRoomHidesTheUnreadDotAndEmptyRoomReadsClearly() {
        const read = Object.assign({}, testCase.room, {unread: false, latest_message: {}});
        const row = createTemporaryObject(factory, testCase, {room: read});
        verify(!findChild(row, "pairUnreadDot").visible);
        compare(findChild(row, "pairPreview").text, "No messages yet");
    }

    function test_renamedParticipantsKeepTheSameRoomIdentity() {
        const renamed = Object.assign({}, testCase.room, {title: "C++ Junior & Hugo Renamed"});
        const row = createTemporaryObject(factory, testCase, {room: renamed});
        compare(row.conversationId, "pair:agent-cpp:agent-hugo");
        compare(findChild(row, "pairTitle").text, "C++ Junior & Hugo Renamed");
    }
}
