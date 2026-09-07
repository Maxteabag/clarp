import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "PreviewVersions"
    when: windowShown
    width: 800
    height: 650
    visible: true
    QtObject {
        id: stub
        property bool busy: false
        property string runningHash: "old"
        property string error: ""
        property string selected: ""
        property var catalog: ({current: "new", latest: "new", pinned: "",
            versions: [{hash: "new", label: "v1.2.3 · abcd1234"}, {hash: "old", label: "Build 1234abcd"}]})
        function refresh() {}
        function selectVersion(hash) { selected = hash; }
    }
    Component {
        id: panel
        Clarp.PreviewVersionPanel { width: 800; height: 650; visible: true; switcher: stub }
    }
    function init() { stub.selected = ""; stub.busy = false; }
    function test_keyboardChoosesExactVersion() {
        const view = createTemporaryObject(panel, testCase);
        waitForRendering(view);
        const old = findChild(view, "previewVersion-old");
        verify(old !== null);
        old.forceActiveFocus();
        keyClick(Qt.Key_Return);
        compare(stub.selected, "old");
    }
    function test_noSwitchDuringRecordingOrAnotherAction() {
        const view = createTemporaryObject(panel, testCase, {canRestart: false});
        waitForRendering(view);
        const item = findChild(view, "previewVersion-new");
        verify(!item.enabled);
        view.canRestart = true;
        stub.busy = true;
        verify(!item.enabled);
        compare(stub.selected, "");
    }
    function test_arrowsAndEnterChooseVersion() {
        const view = createTemporaryObject(panel, testCase);
        waitForRendering(view);
        const list = findChild(view, "previewVersionList");
        list.forceActiveFocus();
        list.currentIndex = 0;
        keyClick(Qt.Key_Down);
        compare(list.currentIndex, 1);
        keyClick(Qt.Key_Return);
        compare(stub.selected, "old");
    }
    function test_escapeDoesNotSwitch() {
        const view = createTemporaryObject(panel, testCase);
        view.forceActiveFocus();
        keyClick(Qt.Key_Escape);
        compare(view.visible, false);
        compare(stub.selected, "");
    }
}
