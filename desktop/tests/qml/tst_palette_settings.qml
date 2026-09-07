import QtQuick
import QtTest
import "../../qml/components" as Clarp
TestCase {
    id: testCase
    name: "PaletteSettings"
    when: windowShown
    visible: true
    width: 720; height: 540
    QtObject {
        id: stub
        property int activityDisplayMode: 0
        property int agentRevision: 0
        property string lastBackend: "codex"
        property string lastWorkingDirectory: "/tmp"
        property string selectedSession: "fixture"
        property bool timestampsVisible: false
        property bool showWhenReady: false
        property bool toolsVisible: false
        property bool pauseMobilePush: true
        property bool sharedFilesystem: false
        property bool muted: false
        property int focusRequests: 0
        property int avatarRevision: 0
        property QtObject contacts: QtObject { property int count: 0 }
        property QtObject panes: QtObject { property string activePaneId: "pane-1" }
        property QtObject toolNarrator: QtObject {
            property bool enabled: true
            property int detailLevel: 2
            property var detailLevels: ["Developer", "Technical", "Balanced", "Plain English", "Grandma"]
        }
        function requestComposerFocus(pane) { focusRequests++; }
        function matchingAgents(query) { return []; }
        function matchingContacts(query) { return []; }
        function quickStartBackend() { return "codex"; }
        function avatarSource(session) { return ""; }
    }
    Component { id: factory; Clarp.QuickSwitcher { width: testCase.width; height: testCase.height; controller: stub } }
    SignalSpy { id: commands; signalName: "commandRequested" }
    function test_toolActivitySearchOffersAllThreeModes() {
        stub.activityDisplayMode = 0;
        const picker = createTemporaryObject(factory, testCase);
        picker.open(true); picker.query = "tool activity";
        compare(picker.results.length, 3);
        verify(picker.results[0].label.includes("Grouped"));
        verify(picker.results[1].label.includes("Always visible"));
        verify(picker.results[2].label.includes("Group old"));
        picker.choose(2);
        compare(stub.activityDisplayMode, 2);
        picker.open(false); picker.query = "group old";
        compare(picker.results.length, 1);
        verify(picker.results[0].label.includes("current"));
    }
    function test_enterChangesSettingAndRestoresComposer() {
        stub.showWhenReady = false;
        stub.focusRequests = 0;
        const picker = createTemporaryObject(factory, testCase);
        commands.target = picker; commands.clear();
        picker.open(true);
        picker.query = "settings streaming";
        compare(picker.results.length, 1);
        verify(picker.results[0].label.includes("Off → On"));
        wait(20);
        keyClick(Qt.Key_Return);
        compare(stub.showWhenReady, true);
        compare(picker.visible, false);
        tryCompare(stub, "focusRequests", 1);
        compare(commands.count, 0); // No navigation to Settings.
        picker.open(false); picker.query = "streaming";
        verify(picker.results[0].label.includes("On → Off"));
        picker.choose(0);
        compare(stub.showWhenReady, false);
    }
    function test_phonePreferenceAndExactDetailLevel() {
        stub.pauseMobilePush = true;
        const picker = createTemporaryObject(factory, testCase);
        picker.open(false); picker.query = "iphone";
        compare(picker.results.length, 1);
        picker.choose(0);
        compare(stub.pauseMobilePush, false);
        picker.open(false); picker.query = "Grandma";
        compare(picker.results.length, 1);
        picker.choose(0);
        compare(stub.toolNarrator.detailLevel, 4);
        picker.open(false); picker.query = "Grandma";
        verify(picker.results[0].label.includes("current"));
        picker.close();
    }
    function test_escapeDoesNotChangeSetting() {
        stub.timestampsVisible = false;
        const picker = createTemporaryObject(factory, testCase);
        picker.open(false); picker.query = "timestamps";
        wait(20);
        keyClick(Qt.Key_Escape);
        compare(picker.visible, false);
        compare(stub.timestampsVisible, false);
    }
}
