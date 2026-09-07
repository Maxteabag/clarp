import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "ContactPicker"
    when: windowShown
    visible: true
    width: 720
    height: 540

    QtObject {
        id: stub
        property int agentRevision: 0
        property string lastBackend: "codex"
        property string lastWorkingDirectory: "/work/project"
        property string selectedSession: ""
        property QtObject contacts: QtObject { property int count: 2 }
        property QtObject panes: QtObject { property string activePaneId: "pane-1" }
        function requestComposerFocus(paneId) {}
        function matchingAgents(query) { return []; }
        function matchingContacts(query) {
            return [{name: "Bella"}, {name: "Theo"}].filter(
                contact => contact.name.toLowerCase().includes(query.toLowerCase()));
        }
        function quickStartBackend() { return lastBackend; }
    }

    Component {
        id: pickerComponent
        Clarp.QuickSwitcher {
            width: testCase.width
            height: testCase.height
            controller: stub
        }
    }
    SignalSpy { id: startSignal; signalName: "contactRequested" }

    function test_idlePickerSearchesAndStartsContact() {
        const picker = createTemporaryObject(pickerComponent, testCase);
        startSignal.target = picker;
        startSignal.clear();
        picker.openContacts(false);
        compare(picker.results.length, 2);
        compare(picker.results[0].kind, "contact");
        compare(picker.results[0].backend, "codex");
        compare(picker.results[0].directory, "/work/project");
        picker.query = "theo";
        compare(picker.results.length, 1);
        picker.choose(0);
        compare(startSignal.count, 1);
        compare(startSignal.signalArguments[0][0], "Theo");
        compare(picker.visible, false);
    }
}
