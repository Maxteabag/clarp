import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "RenameAgent"
    width: 700; height: 500; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool connected: true
        property string errorMessage: ""
        property int calls: 0
        property var captured: []
        signal agentMutationSucceeded(string session)
        function clearError() { errorMessage = ""; }
        function renameAgent(session, name) { calls++; captured = [session, name]; }
    }
    Component {
        id: factory
        RenameAgentDialog { width: testCase.width; height: testCase.height; controller: stub; visible: false }
    }
    SignalSpy { id: closed; signalName: "closeRequested" }
    function init() { stub.calls = 0; stub.connected = true; stub.errorMessage = ""; stub.captured = []; }

    function test_opensOnTheCurrentNameAndRenamesOnce() {
        const dialog = createTemporaryObject(factory, testCase);
        closed.target = dialog; closed.clear();
        dialog.open("fixer", "Fixer");
        const name = findChild(dialog, "renameAgentName");
        tryCompare(name, "activeFocus", true);
        // Pre-filled and selected, so typing replaces rather than appends.
        compare(name.text, "Fixer");
        compare(name.selectedText, "Fixer");
        // Submitting the name it already has is not a rename.
        keyClick(Qt.Key_Return);
        compare(stub.calls, 0);
        compare(closed.count, 1);

        dialog.open("fixer", "Fixer");
        name.text = "  Repair Bot  ";
        keyClick(Qt.Key_Return);
        compare(stub.calls, 1);
        compare(stub.captured, ["fixer", "Repair Bot"]);
        // Held while the Host answers, so Enter twice cannot rename twice.
        dialog.submit(); compare(stub.calls, 1);
        compare(closed.count, 1);
        // Another chat finishing a mutation must not close this dialog.
        stub.agentMutationSucceeded("rachel");
        compare(closed.count, 1);
        stub.agentMutationSucceeded("fixer");
        compare(closed.count, 2);
    }

    function test_rejectionReleasesTheDialogForAnotherTry() {
        const dialog = createTemporaryObject(factory, testCase);
        closed.target = dialog; closed.clear();
        dialog.open("fixer", "Fixer");
        const name = findChild(dialog, "renameAgentName");
        tryCompare(name, "activeFocus", true);
        name.text = "Rachel";
        keyClick(Qt.Key_Return);
        compare(stub.calls, 1);
        // A name another session already holds comes back as an error; the
        // typed text survives so it can be corrected in place.
        stub.errorMessage = "Rachel already has an active session.";
        compare(dialog.submitting, false);
        compare(name.text, "Rachel");
        compare(closed.count, 0);
        name.text = "Rachel II";
        dialog.submit();
        compare(stub.calls, 2);
        compare(stub.captured, ["fixer", "Rachel II"]);
    }

    function test_escapeAndOfflineDoNotRename() {
        const dialog = createTemporaryObject(factory, testCase);
        closed.target = dialog; closed.clear();
        dialog.open("fixer", "Fixer");
        const name = findChild(dialog, "renameAgentName");
        tryCompare(name, "activeFocus", true);
        name.text = "Offline Name";
        stub.connected = false;
        keyClick(Qt.Key_Return); compare(stub.calls, 0);
        compare(findChild(dialog, "renameAgentSubmit").enabled, false);
        stub.connected = true;
        name.text = "   ";
        keyClick(Qt.Key_Return); compare(stub.calls, 0);
        compare(findChild(dialog, "renameAgentSubmit").enabled, false);
        keyClick(Qt.Key_Escape); compare(closed.count, 1);
    }
}
