import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "QuickNewAgent"
    width: 700; height: 500; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool connected: true
        property string errorMessage: ""
        property string lastWorkingDirectory: "/work/project"
        property int calls: 0
        property var captured: []
        signal agentMutationSucceeded()
        function clearError() { errorMessage = ""; }
        function quickStartBackend() { return "codex"; }
        function createAgent(name, cwd, backend, model, effort, replace, mode, past, mcp) {
            calls++; captured = [name, cwd, backend, model, effort, replace, mode, past, mcp];
        }
    }
    Component {
        id: factory
        QuickNewAgentDialog { width: testCase.width; height: testCase.height; controller: stub; visible: false }
    }
    SignalSpy { id: closed; signalName: "closeRequested" }
    function init() { stub.calls = 0; stub.connected = true; stub.errorMessage = ""; }
    function test_nameEnterStartsOnceAndWaitsForSuccess() {
        const dialog = createTemporaryObject(factory, testCase);
        closed.target = dialog; closed.clear();
        dialog.visible = true;
        const name = findChild(dialog, "quickNewAgentName");
        tryCompare(name, "activeFocus", true);
        keyClick(Qt.Key_Return);
        compare(stub.calls, 0);
        name.text = "  New Friend  ";
        keyClick(Qt.Key_Return);
        compare(stub.calls, 1);
        compare(stub.captured, ["New Friend", "~", "codex", "", "", "", "fresh", "", []]);
        dialog.submit(); compare(stub.calls, 1);
        compare(closed.count, 0);
        stub.errorMessage = "Name already in use";
        compare(dialog.submitting, false);
        compare(name.text, "  New Friend  ");
        dialog.submit(); compare(stub.calls, 2);
        stub.agentMutationSucceeded(); compare(closed.count, 1);
    }
    function test_cancelAndOfflineDoNotCreate() {
        const dialog = createTemporaryObject(factory, testCase);
        closed.target = dialog; closed.clear();
        dialog.visible = true;
        const name = findChild(dialog, "quickNewAgentName");
        tryCompare(name, "activeFocus", true);
        name.text = "Other";
        stub.connected = false;
        keyClick(Qt.Key_Return); compare(stub.calls, 0);
        keyClick(Qt.Key_Escape); compare(closed.count, 1);
    }
}
