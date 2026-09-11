import QtQuick
import QtTest
import "../../qml/components"
TestCase {
    id: testCase
    name: "LaunchKeyboard"
    width: 820; height: 640; visible: true
    when: windowShown
    QtObject {
        id: stub
        property bool connected: true
        property bool anonymousAgents: true
        property string lastBackend: "codex"
        property string errorMessage: ""
        property string lastWorkingDirectory: "/fixture"
        property int starts: 0
        property var args: []
        signal modelCatalogChanged()
        signal launchPoolEmpty()
        signal agentMutationSucceeded()
        function clearError() { errorMessage = ""; }
        function quickStartBackend() { return lastBackend; }
        function modelsForBackend(backend) { return [{id:"",label:"Default"},{id:"model-one",label:"Model One"},{id:"model-two",label:"Model Two"}]; }
        function startAnonymousAgent(backend, model, effort) { starts++; args=[backend,model,effort]; return true; }
        function startAvailableContact(backend, model, effort) { return startAnonymousAgent(backend,model,effort); }
        function createAgent(name,cwd,backend,model,effort) { starts++; args=[backend,model,effort,name]; }
    }
    Component { id: factory; LaunchAgentPage { controller: stub; visible: false; width: 820; height: 640 } }
    function init() { stub.starts=0; stub.args=[]; stub.connected=true; stub.errorMessage=""; stub.lastBackend="codex"; }
    function opened() { const d=createTemporaryObject(factory,testCase); d.open("","",""); wait(30); return d; }
    function test_providerKeys_data() {
        return [
            {tag:"default-1-key",keys:[],backend:"codex"},
            {tag:"claude-2-keys",keys:[Qt.Key_Left],backend:"claude"},
            {tag:"grok-2-keys",keys:[Qt.Key_Right],backend:"grok"},
            {tag:"agy-3-keys",keys:[Qt.Key_Right,Qt.Key_Right],backend:"agy"},
            {tag:"opencode-wrap-3-keys",keys:[Qt.Key_Left,Qt.Key_Left],backend:"opencode"},
            {tag:"tab-selects-provider",keys:[Qt.Key_Tab],backend:"grok"},
            {tag:"down-selects-provider",keys:[Qt.Key_Down],backend:"grok"}
        ];
    }
    function test_providerKeys(data) {
        const d=opened();
        for (const key of data.keys) keyClick(key);
        keyClick(Qt.Key_Return);
        compare(stub.starts,1);
        compare(stub.args,[data.backend,"",""]);
        keyClick(Qt.Key_Return); compare(stub.starts,1);
    }
    function test_optionalModelThreeKeys() {
        const d=opened();
        keyClick(Qt.Key_M); keyClick(Qt.Key_Down); keyClick(Qt.Key_Return);
        compare(stub.starts,1); compare(stub.args,["codex","model-one",""]);
    }
    function test_retryRestoresCardFocus() {
        const d=opened(); keyClick(Qt.Key_Return);
        compare(stub.starts,1);
        stub.errorMessage="Temporary error"; wait(30);
        keyClick(Qt.Key_Return);
        compare(stub.starts,2); compare(stub.args,["codex","",""]);
    }
    function test_escapeDoesNotStart() {
        const d=opened();
        const spy=createTemporaryQmlObject('import QtTest; SignalSpy {}',testCase);
        spy.target=d; spy.signalName="closeRequested";
        keyClick(Qt.Key_Escape); compare(stub.starts,0); compare(spy.count,1);
    }
    function test_backtabSelectsPreviousProvider() {
        const d=opened(); keyClick(Qt.Key_Backtab); keyClick(Qt.Key_Return);
        compare(stub.args,["claude","",""]);
    }
    function test_escapeModelPickerReturnsToCards() {
        const d=opened(); keyClick(Qt.Key_M); keyClick(Qt.Key_Escape);
        compare(d.choosingModel,false); keyClick(Qt.Key_Return);
        compare(stub.starts,1); compare(stub.args,["codex","",""]);
    }
    function test_offlineEnterDoesNotCreate() {
        stub.connected=false;
        const d=opened(); keyClick(Qt.Key_Return); compare(stub.starts,0);
        stub.connected=true; keyClick(Qt.Key_Return); compare(stub.starts,1);
    }
    function test_poolEmptyFocusesNameForTyping() {
        stub.anonymousAgents=false;
        const d=opened(); keyClick(Qt.Key_Return);
        stub.launchPoolEmpty(); wait(30);
        keyClick(Qt.Key_N); keyClick(Qt.Key_O); keyClick(Qt.Key_V); keyClick(Qt.Key_A); keyClick(Qt.Key_Return);
        compare(stub.args,["codex","","","nova"]);
        stub.anonymousAgents=true;
    }

    function test_cancelPendingDirectLaunchStaysCancelledAfterConnection() {
        stub.connected=false;
        const d=createTemporaryObject(factory,testCase); d.open("codex","",""); wait(30);
        keyClick(Qt.Key_Escape); stub.connected=true; wait(30);
        compare(stub.starts,0);
    }

}
