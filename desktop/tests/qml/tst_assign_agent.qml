import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "AssignAgent"
    width: 700; height: 700; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool connected: true
        property string errorMessage: ""
        property var assignmentContacts: [{name: "Friend"}]
        property int calls: 0
        property var args: []
        signal contactAssignmentSucceeded(string session)
        function clearError() { errorMessage = ""; }
        function loadAssignmentContacts(session) {}
        function assignContact(session, mode, name) { calls++; args = [session, mode, name]; }
    }
    Component { id: factory; AssignAgentDialog { controller: stub; visible: false; width: 700; height: 700 } }
    function init() { stub.calls = 0; stub.errorMessage = ""; }
    function test_defaultAutomaticAndSingleSubmit() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("same-session", false);
        compare(dialog.mode, "auto"); compare(stub.calls, 0);
        tryCompare(findChild(dialog, "assignAutomatically"), "activeFocus", true);
        keyClick(Qt.Key_Return); dialog.submit();
        compare(stub.calls, 1); compare(stub.args, ["same-session", "auto", ""]);
    }
    function test_autoShortcutAndRecoveryToCreate() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("same-session", true);
        compare(stub.calls, 1);
        stub.errorMessage = "No contacts available";
        verify(!dialog.submitting);
        dialog.mode = "create";
        dialog.submit(); compare(stub.calls, 1);
        findChild(dialog, "assignmentNewName").text = "New Contact";
        dialog.submit(); compare(stub.args, ["same-session", "create", "New Contact"]);
    }
    function test_chooseContact() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("same-session", false);
        dialog.mode = "choose";
        dialog.submit();
        compare(stub.args, ["same-session", "choose", "Friend"]);
    }
}
