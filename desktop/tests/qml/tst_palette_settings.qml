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
        property bool minimalUi: false
        property bool workspaceBarVisible: true
        property bool showWhenReady: false
        property bool toolsVisible: false
        property bool pauseMobilePush: true
        property bool sharedFilesystem: false
        property bool muted: false
        property string readingTheme: "terminal"
        property var readingThemes: [
            {id: "terminal", label: "Terminal", detail: "Mono", fontFamily: "JetBrains Mono"},
            {id: "paper", label: "Paper", detail: "Sepia serif", fontFamily: "Literata"},
            {id: "dusk", label: "Dusk", detail: "Dark serif", fontFamily: "Literata"}
        ]
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
        property var agentList: []
        function matchingAgents(query) { return agentList; }
        function matchingContacts(query) { return []; }
        function quickStartBackend() { return "codex"; }
        function avatarSource(session) { return ""; }
    }
    Component { id: factory; Clarp.QuickSwitcher { width: testCase.width; height: testCase.height; controller: stub } }
    SignalSpy { id: commands; signalName: "commandRequested" }
    SignalSpy { id: agentChoices; signalName: "agentRequested" }
    function init() { stub.agentList = []; }
    function test_restingPointerDoesNotStealTheSelection() {
        // A pointer resting where the switcher opened hovered whichever row
        // slid under it as results changed; hover selected it and Enter
        // applied a reading theme instead of opening the agent typed.
        stub.agentList = [
            {session: "ada", name: "Ada", backend: "claude", state: "idle", busy: false, unread: false},
            {session: "adam", name: "Adam", backend: "claude", state: "idle", busy: false, unread: false}];
        const picker = createTemporaryObject(factory, testCase);
        agentChoices.target = picker; agentChoices.clear();
        commands.target = picker; commands.clear();
        picker.open(false);
        mouseMove(picker, picker.width / 2, picker.height * 0.62);
        wait(20);
        keyClick("a"); keyClick("d"); keyClick("a");
        wait(20);
        keyClick(Qt.Key_Return);
        compare(agentChoices.count, 1, "Enter opens the top match, not the row under a resting pointer");
        compare(agentChoices.signalArguments[0][0], "ada");
        stub.agentList = [];
    }
    function test_movingPointerStillSelectsARow() {
        stub.agentList = [
            {session: "ada", name: "Ada", backend: "claude", state: "idle", busy: false, unread: false},
            {session: "adam", name: "Adam", backend: "claude", state: "idle", busy: false, unread: false}];
        const picker = createTemporaryObject(factory, testCase);
        agentChoices.target = picker; agentChoices.clear();
        picker.open(false);
        keyClick("a"); keyClick("d"); keyClick("a");
        wait(20);
        // Move onto the second result row (Adam) with a real pointer movement.
        const rows = [];
        const stack = [picker];
        while (stack.length) { const it = stack.pop(); for (const c of it.children) stack.push(c); if (it.modelData !== undefined && it.index === 1) rows.push(it); }
        verify(rows.length > 0, "second result row exists");
        const row = rows[0];
        mouseMove(row, 10, row.height / 2);
        wait(10);
        mouseMove(row, 40, row.height / 2);
        wait(20);
        keyClick(Qt.Key_Return);
        compare(agentChoices.count, 1);
        compare(agentChoices.signalArguments[0][0], "adam");
    }
    function test_enterOpensTheSelectedAgentWhileResultsRebuild() {
        // While an agent streams, the results rebuild several times a second.
        // Each rebuild reset the selection, so Enter chose nothing and the
        // switch silently did not happen.
        stub.agentList = [
            {session: "ada", name: "Ada", backend: "claude", state: "running", busy: true, unread: false},
            {session: "bram", name: "Bram", backend: "claude", state: "idle", busy: false, unread: false}];
        const picker = createTemporaryObject(factory, testCase);
        agentChoices.target = picker; agentChoices.clear();
        picker.open(false);
        keyClick("a");
        keyClick(Qt.Key_Down); // Bram
        for (let i = 0; i < 5; ++i) { stub.agentRevision++; wait(5); }
        keyClick(Qt.Key_Return);
        compare(agentChoices.count, 1);
        compare(agentChoices.signalArguments[0][0], "bram");
        stub.agentList = [];
    }
    function test_minimalUiIsAnIndependentToggle() {
        stub.minimalUi = false;
        const picker = createTemporaryObject(factory, testCase);
        picker.open(true); picker.query = "minimal ui";
        compare(picker.results.length, 1);
        picker.choose(0);
        compare(stub.minimalUi, true);
        picker.open(false); picker.query = "minimal ui";
        verify(picker.results[0].label.includes("On → Off"));
        picker.choose(0);
        compare(stub.minimalUi, false);
    }
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
    function test_readingThemeOffersEveryThemeAndMarksCurrent() {
        stub.readingTheme = "terminal";
        const picker = createTemporaryObject(factory, testCase);
        picker.open(true); picker.query = "reading theme";
        compare(picker.results.length, 3);
        verify(picker.results[0].label.includes("Terminal"));
        verify(picker.results[0].label.includes("current"));
        verify(!picker.results[1].label.includes("current"));
        picker.choose(1);
        compare(stub.readingTheme, "paper");
        picker.open(false); picker.query = "sepia";
        compare(picker.results.length, 1);
        verify(picker.results[0].label.includes("Paper"));
        verify(picker.results[0].label.includes("current"));
        picker.open(false); picker.query = "font literata";
        compare(picker.results.length, 2);
        picker.close();
    }
    function test_workspaceBarIsAToggle() {
        stub.workspaceBarVisible = true;
        const picker = createTemporaryObject(factory, testCase);
        picker.open(false); picker.query = "workspace bar";
        compare(picker.results.length, 1);
        verify(picker.results[0].label.includes("On → Off"));
        picker.choose(0);
        compare(stub.workspaceBarVisible, false);
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
