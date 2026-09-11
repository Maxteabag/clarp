import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "LaunchDirectory"
    width: 820; height: 720; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool connected: true
        property bool anonymousAgents: true
        property string lastBackend: "codex"
        property string lastWorkingDirectory: "/wrong-previous-project"
        property string errorMessage: ""
        property var launchDirectories: []
        property bool launchDirectoriesLoading: false
        property string query: ""
        property string chosenDirectory: ""
        property int starts: 0
        property var args: []
        signal modelCatalogChanged()
        signal launchPoolEmpty()
        signal agentMutationSucceeded()
        function clearError() { errorMessage=""; }
        function modelsForBackend(backend) { return [{id:"",label:"Default"}]; }
        function setLaunchDirectory(path) { chosenDirectory=path; }
        function loadLaunchDirectories(value) { query=value; launchDirectoriesLoading=true; launchDirectoriesChanged(); }
        function respond(values) { launchDirectories=values; launchDirectoriesLoading=false; launchDirectoriesChanged(); }
        function startAnonymousAgent(backend, model, effort) { starts++; args=[backend,chosenDirectory]; return true; }
        function startAvailableContact(backend, model, effort) { return startAnonymousAgent(backend,model,effort); }
    }
    Component { id: factory; LaunchAgentPage { controller:stub; visible:false; width:820; height:720 } }
    function init() { stub.starts=0; stub.args=[]; stub.launchDirectories=[]; stub.launchDirectoriesLoading=false; stub.errorMessage=""; }
    function open(backend) {
        const page=createTemporaryObject(factory,testCase);page.open(backend || "","","");wait(30);
        tryCompare(findChild(page,"launchDirectorySearch"),"activeFocus",true);
        return page;
    }
    function test_homeThenBackendWithTwoEnters() {
        const page=open();compare(stub.starts,0);
        keyClick(Qt.Key_Return);wait(20);verify(!page.choosingDirectory);
        compare(stub.chosenDirectory,"~");compare(stub.starts,0);
        keyClick(Qt.Key_Return);compare(stub.args,["codex","~"]);
    }
    function test_recentDirectoryByArrowThenEnter() {
        const page=open();
        stub.respond([{path:"/home/test",label:"~"},{path:"/work/recent",label:"/work/recent"}]);
        keyClick(Qt.Key_Down);keyClick(Qt.Key_Return);wait(20);keyClick(Qt.Key_Return);
        compare(stub.args,["codex","/work/recent"]);
    }
    function test_enterWaitsForNewestFuzzyResults() {
        const page=open();
        stub.respond([{path:"/home/test",label:"~"}]);
        keyClick(Qt.Key_C);keyClick(Qt.Key_L);keyClick(Qt.Key_A);keyClick(Qt.Key_R);keyClick(Qt.Key_P);
        keyClick(Qt.Key_Return);compare(stub.starts,0);verify(page.choosingDirectory);
        compare(stub.query,"clarp");
        stub.respond([{path:"/work/clarp",label:"/work/clarp"}]);wait(20);
        verify(!page.choosingDirectory);keyClick(Qt.Key_Return);
        compare(stub.args,["codex","/work/clarp"]);
    }
    function test_backendShortcutStillAsksDirectory() {
        const page=open("claude");compare(stub.starts,0);
        keyClick(Qt.Key_Return);wait(20);compare(stub.args,["claude","~"]);
    }
    function test_explicitDirectorySkipsPicker() {
        const page=createTemporaryObject(factory,testCase);page.open("codex","","",undefined,"/explicit");wait(20);
        compare(stub.args,["codex","/explicit"]);verify(!page.choosingDirectory);
    }
    function test_noMatchNeverFallsBackToOldDirectory() {
        const page=open();keyClick(Qt.Key_Z);keyClick(Qt.Key_Return);
        stub.respond([]);wait(20);keyClick(Qt.Key_Return);
        verify(page.choosingDirectory);compare(stub.starts,0);
    }
}
