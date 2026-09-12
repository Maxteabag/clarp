import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "LaunchAgent"
    width: 700; height: 700; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool anonymousAgents: false
        property var launchDirectories: []
        property bool launchDirectoriesLoading: false
        function loadLaunchDirectories(query) {}
        function setLaunchDirectory(path) {}
        property bool connected: true
        property string errorMessage: ""
        property string lastWorkingDirectory: "/workspace"
        property int starts: 0
        property int creates: 0
        property var args: []
        signal modelCatalogChanged()
        signal launchPoolEmpty()
        signal agentMutationSucceeded()
        function clearError() { errorMessage = ""; }
        function modelsForBackend(backend) { return [{id: "", label: "Provider default"}]; }
        function startAnonymousAgent(backend, model, effort) { starts++; args = ["anonymous", backend, model, effort]; return true; }
        function startAvailableContact(backend, model, effort) { starts++; args = [backend, model, effort]; return true; }
        function createAgent(name, cwd, backend, model, effort) { creates++; args = [name, cwd, backend, model, effort]; }
    }
    Component { id: factory; LaunchAgentPage { controller: stub; visible: false; width: 700; height: 700 } }
    function init() { stub.starts = 0; stub.creates = 0; stub.connected = true; stub.errorMessage = ""; }
    function test_homeDefaultAndDirectoryOverride() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("", "", "", undefined, ""); wait(20);
        compare(dialog.directory, "~"); compare(dialog.choosingDirectory, false);
        dialog.changeDirectory(); wait(20);
        compare(dialog.choosingDirectory, true); compare(stub.starts, 0);
        dialog.open("codex", "", "", undefined, ""); wait(20);
        compare(dialog.directory, "~"); compare(stub.starts, 1);
        dialog.open("", "", "", undefined, "/explicit"); wait(20);
        compare(dialog.directory, "/explicit");
    }
    function test_anonymousSettingAndFlags() {
        const dialog = createTemporaryObject(factory, testCase);
        stub.anonymousAgents = true;
        dialog.open("codex", "my-model", "high", undefined, "/workspace"); wait(20);
        compare(stub.args, ["anonymous", "codex", "my-model", "high"]);
        dialog.open("codex", "my-model", "high", 0, "/workspace"); wait(20);
        compare(stub.args, ["codex", "my-model", "high"]);
        stub.anonymousAgents = false;
    }
    function test_chooserDoesNotCreateUntilBackendSelected() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("", "", "", undefined, "/workspace"); wait(20);
        compare(stub.starts, 0); compare(stub.creates, 0);
        dialog.backend = "claude";
        dialog.submit(); dialog.submit();
        compare(stub.starts, 1);
        compare(stub.args, ["claude", "", ""]);
    }
    function test_flagsWaitForConnectionAndEmptyPoolOffersCreation() {
        stub.connected = false;
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("codex", "test-model", "high", undefined, "/workspace"); wait(20);
        compare(stub.starts, 0);
        stub.connected = true;
        compare(stub.starts, 1);
        compare(stub.args, ["codex", "test-model", "high"]);
        stub.launchPoolEmpty();
        verify(dialog.poolEmpty); compare(stub.creates, 0);
        dialog.submit(); compare(stub.creates, 0);
        findChild(dialog, "launchContactName").text = "New Friend";
        dialog.submit(); dialog.submit();
        compare(stub.creates, 1);
        compare(stub.args, ["New Friend", "/workspace", "codex", "test-model", "high"]);
    }
    function test_errorAllowsRetryWithoutLosingModel() {
        const dialog = createTemporaryObject(factory, testCase);
        dialog.open("grok", "chosen-model", "", undefined, "/workspace"); wait(20);
        compare(stub.starts, 1);
        stub.errorMessage = "Contact already occupied";
        verify(!dialog.submitting);
        dialog.submit(); compare(stub.starts, 2);
        compare(stub.args, ["grok", "chosen-model", ""]);
    }
}
