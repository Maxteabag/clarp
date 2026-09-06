import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "SidebarCompletedPreview"
    width: 360; height: 200; visible: true
    when: windowShown
    function init() { stubController.showWhenReady = true; }
    QtObject {
        id: stubController
        property bool showWhenReady: true
        property string selectedSession: "fixture"
        property int avatarRevision: 0
        function avatarSource(session) { return ""; }
        function chatStamp(timestamp) { return ""; }
    }
    Component {
        id: rowComponent
        ChatRow {
            controller: stubController; session: "fixture"; name: "Fixture"; backend: "local"
            workingDirectory: "/tmp"; avatarUrl: ""; lastMessage: "Unfinished text"
            lastCompletedMessage: "Previous completed reply"; agentState: "thinking"
            statusText: ""; lastActivity: 0; busy: true; unread: false; muted: false; queueCount: 0
        }
    }
    function test_completedPreviewAndWorkingAreSeparate() {
        const row = createTemporaryObject(rowComponent, testCase);
        verify(row !== null);
        const preview = findChild(row, "sidebarMessagePreview");
        compare(preview.text, "Previous completed reply");
        compare(row.activityLine, "Working…");
        row.lastMessage = "More unfinished text";
        compare(preview.text, "Previous completed reply");
        row.lastCompletedMessage = "New finished reply";
        row.busy = false;
        compare(preview.text, "New finished reply");
        compare(row.activityLine, "");
        stubController.showWhenReady = false;
        compare(preview.text, "More unfinished text");
        stubController.showWhenReady = true;
        row.lastCompletedMessage = "";
        compare(preview.text, "/tmp");
    }
    function test_activityLabelDoesNotDependOnAnswerPresentation() {
        const row = createTemporaryObject(rowComponent, testCase);
        for (const state of ["thinking", "tool", "compacting", "running"]) {
            row.agentState = state;
            for (const ready of [false, true]) {
                stubController.showWhenReady = ready;
                compare(row.activityLine, "Working…");
                row.statusText = "Running tests";
                compare(row.activityLine, "Running tests");
                row.statusText = "";
            }
        }
        row.busy = false;
        row.agentState = "done";
        compare(row.activityLine, "");
        stubController.showWhenReady = true;
    }
}
