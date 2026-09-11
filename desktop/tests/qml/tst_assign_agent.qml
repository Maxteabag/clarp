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
        property var assignmentContacts: [{name: "Friend"}, {name: "Other Friend"}]
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
    function test_keyboardChoosesContactWithoutMouse() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("same-session", false);
        wait(30);
        keyClick(Qt.Key_Down);
        compare(dialog.mode, "choose");
        keyClick(Qt.Key_Tab);
        keyClick(Qt.Key_Down);
        keyClick(Qt.Key_Return);
        compare(stub.calls, 1);
        compare(stub.args, ["same-session", "choose", "Other Friend"]);
    }
    function test_keyboardCreatesContactWithoutMouse() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("same-session", false);
        wait(30);
        keyClick(Qt.Key_Down); keyClick(Qt.Key_Down); keyClick(Qt.Key_Return);
        tryCompare(findChild(dialog, "assignmentNewName"), "activeFocus", true);
        keyClick(Qt.Key_N); keyClick(Qt.Key_O); keyClick(Qt.Key_V); keyClick(Qt.Key_A);
        keyClick(Qt.Key_Return);
        compare(stub.calls, 1);
        compare(stub.args, ["same-session", "create", "nova"]);
    }

}
