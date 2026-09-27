import QtQuick
import QtTest
import "../../qml/components" as Clarp
TestCase {
    id: testCase
    name: "NewSessionHub"
    when: windowShown
    visible: true
    width: 900; height: 720
    property int hubHeight: 720
    QtObject {
        id: stub
        property bool connected: true
        property string errorMessage: ""
        property string startingContact: ""
        property string lastBackend: "codex"
        property string lastWorkingDirectory: "/home/peter/GIT/clarp"
        property bool anonymousAgents: true
        property int agentRevision: 0
        property var backendOptions: [{id: "claude", label: "Claude"}, {id: "codex", label: "Codex"}, {id: "grok", label: "Grok"}]
        property var launchDirectories: []
        property bool launchDirectoriesLoading: false
        property QtObject contacts: QtObject { property int count: 2 }
        property QtObject panes: QtObject { property string activePaneId: "pane-1" }
        property var avatarMotion: QtObject { property bool reducedMotion: true; property int revision: 0; function working(s) { return false; } function phase(s) { return 0; } function observe(o, v) {} }
        property int avatarRevision: 0
        property var calls: []
        property var contactRows: [{name: "Nadia", description: "Calm planner", symbol: "N"}, {name: "Theo", description: "Builder", symbol: "T"}]
        property var contactAvatarNames: []
        property string launchDirectory: ""
        property string selected: ""
        signal modelCatalogChanged()
        signal launchDefaultsChanged()
        signal agentMutationSucceeded(string session)
        signal contactLaunchChanged()
        signal launchPoolEmpty()
        function clearError() { errorMessage = ""; }
        function requestComposerFocus(pane) {}
        function avatarSource(session) { return ""; }
        function contactAvatarSource(name) { contactAvatarNames.push(name); return ""; }
        function modelsForBackend(backend) { return backend === "claude" ? [{id: "opus", label: "Opus"}, {id: "sonnet", label: "Sonnet"}] : []; }
        function effortsForModel(backend, model) { return model === "opus" ? [{id: "high", label: "High"}] : []; }
        function matchingContacts(query) {
            return contactRows
                .filter(c => c.name.toLowerCase().includes(String(query).trim().toLowerCase()));
        }
        function matchingAgents(query) {
            return [{session: "marcus-4e32", name: "Marcus", backend: "codex", state: "idle", busy: false, unread: false}]
                .filter(a => a.name.toLowerCase().includes(String(query).trim().toLowerCase()));
        }
        function setLaunchDirectory(path) { launchDirectory = path; }
        function quickStartContact(name, backend, model, effort) { calls.push(["quickStartContact", name, backend, model, effort]); startingContact = name; return true; }
        function createAgent(name, dir, backend, model, effort, replace, mode, past, mcp) { calls.push(["createAgent", name, dir, backend, model, effort, mode]); }
        function selectSession(session) { selected = session; }
        function startAnonymousAgent(b, m, e) { calls.push(["startAnonymousAgent", b, m, e]); return true; }
        function startAvailableContact(b, m, e) { calls.push(["startAvailableContact", b, m, e]); return true; }
        function loadLaunchDirectories(q) {}
    }
    Component { id: factory; Clarp.NewSessionHub { width: testCase.width; height: testCase.hubHeight; controller: stub } }
    function init() {
        testCase.hubHeight = 720;
        stub.calls = [];
        stub.contactRows = [{name: "Nadia", description: "Calm planner", symbol: "N"}, {name: "Theo", description: "Builder", symbol: "T"}];
        stub.contactAvatarNames = [];
        stub.startingContact = "";
        stub.errorMessage = "";
        stub.connected = true;
        stub.selected = "";
        stub.lastBackend = "codex";
        stub.lastWorkingDirectory = "/home/peter/GIT/clarp";
    }
    function opened() {
        const hub = createTemporaryObject(factory, testCase);
        hub.open(false, false);
        wait(20);
        return hub;
    }
    function test_defaultProviderComesFromLastBackend() {
        const hub = opened();
        compare(hub.backend, "codex");
        stub.lastBackend = "nope";
        hub.open(false, false);
        compare(hub.backend, "claude"); // First offered when the saved one is gone.
    }
    function test_providerChangeResetsModelAndSummary() {
        const hub = opened();
        hub.selectProvider(0); // claude
        hub.modelId = "opus"; hub.effort = "high";
        compare(hub.modelSummary, "Opus · High");
        keyClick(Qt.Key_Right, Qt.ControlModifier);
        compare(hub.backend, "codex");
        compare(hub.modelId, ""); compare(hub.effort, "");
        compare(hub.modelSummary, "Server default");
    }
    function test_searchFiltersAndEnterStartsIdleContact() {
        const hub = opened();
        compare(hub.rows.length, 2);
        keyClick(Qt.Key_T); keyClick(Qt.Key_H);
        tryCompare(hub, "query", "th");
        compare(hub.rows.length, 1);
        verify(hub.selectedRow !== null && hub.selectedRow.name === "Theo");
        keyClick(Qt.Key_Return);
        compare(stub.calls.length, 1);
        compare(stub.calls[0], ["quickStartContact", "Theo", "codex", "", ""]);
        compare(stub.launchDirectory, "/home/peter/GIT/clarp");
        verify(hub.submitting);
        stub.startingContact = ""; stub.contactLaunchChanged();
        tryCompare(hub, "visible", false);
    }
    function test_contactLaunchUsesDialogModelBackendAndDirectory() {
        const hub = opened();
        hub.selectProvider(0); // claude
        hub.modelId = "opus";
        hub.effort = "high";
        hub.directory = "/data/workspace/dialog";
        keyClick(Qt.Key_Return);
        compare(stub.calls.length, 1);
        compare(stub.calls[0], ["quickStartContact", "Nadia", "claude", "opus", "high"]);
        compare(stub.launchDirectory, "/data/workspace/dialog");
    }
    function test_directoryEnterAppliesTypedPathWithoutSubmitting() {
        const hub = opened();
        hub.choosingDirectory = true;
        wait(20);
        const field = findChild(hub, "launchDirectorySearch");
        verify(field !== null);
        field.forceActiveFocus();
        field.text = "/data/workspace/typed";
        keyClick(Qt.Key_Return);
        compare(hub.visible, true);
        compare(hub.choosingDirectory, false);
        compare(hub.directory, "/data/workspace/typed");
        compare(stub.launchDirectory, "/data/workspace/typed");
        compare(stub.calls.length, 0);
    }
    function test_openHubAdoptsHostDefaultDirectoryWhenItArrives() {
        stub.lastWorkingDirectory = "~";
        const hub = opened();
        compare(hub.directory, "~");
        stub.lastWorkingDirectory = "/data/workspace";
        stub.launchDefaultsChanged();
        compare(hub.directory, "/data/workspace");
        compare(stub.launchDirectory, "/data/workspace");

        hub.directory = "/data/workspace/custom";
        stub.lastWorkingDirectory = "/data/workspace/other";
        stub.launchDefaultsChanged();
        compare(hub.directory, "/data/workspace/custom");
    }
    function test_failedCreateKeepsChosenModel() {
        const hub = opened();
        hub.selectProvider(0); // claude
        hub.modelId = "opus";
        hub.effort = "high";
        keyClick(Qt.Key_Return);
        verify(hub.submitting);
        stub.errorMessage = "path /home/clarp is outside the Clarp workspace root /data/workspace";
        compare(hub.visible, true);
        compare(hub.backend, "claude");
        compare(hub.modelId, "opus");
        compare(hub.effort, "high");
        compare(hub.modelSummary, "Opus · High");
    }
    function test_showAllListsChatsAndEnterOpensThem() {
        const hub = opened();
        hub.showAll = true;
        compare(hub.rows.length, 3);
        verify(hub.rows[2].inChat);
        hub.selectedKey = hub.rows[2].key;
        keyClick(Qt.Key_Return);
        compare(stub.selected, "marcus-4e32");
        compare(stub.calls.length, 0);
        compare(hub.visible, false);
    }
    function test_newContactNameCreatesFreshAgent() {
        const hub = opened();
        hub.newContact = true;
        const field = findChild(hub, "newContactName");
        field.forceActiveFocus();
        field.text = "Mo";
        keyClick(Qt.Key_Return);
        compare(stub.calls.length, 1);
        compare(stub.calls[0], ["createAgent", "Mo", "/home/peter/GIT/clarp", "codex", "", "", "fresh"]);
        stub.agentMutationSucceeded("mo-1");
        tryCompare(hub, "visible", false);
    }
    function test_escapeClosesWithoutStarting() {
        const hub = opened();
        keyClick(Qt.Key_Escape);
        compare(hub.visible, false);
        compare(stub.calls.length, 0);
    }
    function test_escapeStepsOutOfEditorsFirst() {
        const hub = opened();
        hub.editingModel = true;
        keyClick(Qt.Key_Escape);
        compare(hub.editingModel, false); compare(hub.visible, true);
        keyClick(Qt.Key_Escape);
        compare(hub.visible, false);
    }
    function test_disconnectedShowsConnectMessageAndDisablesConfirm() {
        stub.connected = false;
        const hub = opened();
        compare(hub.canConfirm, false);
        keyClick(Qt.Key_Return);
        compare(stub.calls.length, 0);
    }
    function test_commandLineLaunchStartsImmediately() {
        const hub = createTemporaryObject(factory, testCase);
        hub.openLaunch("claude", "opus", "high", 1, "/tmp/work");
        compare(stub.calls.length, 1);
        compare(stub.calls[0], ["startAnonymousAgent", "claude", "opus", "high"]);
        compare(stub.launchDirectory, "/tmp/work");
    }
    function test_contactPortraitsWaitUntilCardsAreVisible() {
        testCase.hubHeight = 260;
        stub.contactRows = Array.from({length: 24}, (_, i) => ({
            name: "Contact " + String(i).padStart(2, "0"), description: "Lazy contact", symbol: "C"
        }));
        const hub = opened();
        wait(80);
        verify(stub.contactAvatarNames.length > 0);
        verify(stub.contactAvatarNames.length < stub.contactRows.length);
        compare(stub.contactAvatarNames.indexOf("Contact 23"), -1);
        const cards = findChild(hub, "contactCards");
        verify(cards !== null);
        cards.contentY = cards.contentHeight - cards.height;
        wait(80);
        verify(stub.contactAvatarNames.indexOf("Contact 23") >= 0);
    }
    function test_errorReenablesConfirm() {
        const hub = opened();
        keyClick(Qt.Key_Return);
        verify(hub.submitting);
        stub.errorMessage = "Host refused";
        compare(hub.submitting, false);
        compare(hub.visible, true);
    }
}
