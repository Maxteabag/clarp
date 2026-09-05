import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "SettingsKeyboard"
    when: windowShown
    visible: true
    width: 760
    height: 480

    QtObject {
        id: stub
        property bool timestampsVisible: false
        property bool toolsVisible: false
        property bool muted: false
        property bool sharedFilesystem: false
        property QtObject toolNarrator: QtObject {
            property bool enabled: false
            property string status: "Off"
        }
        property string serverName: "Test Host"
        property string baseUrl: "http://localhost:1"
        property string connectionState: "offline"
        property string serverVersion: "test"
        property bool settingsStatusLoading: false
        property var diagnosticsHealth: ({ready: true})
        property var transcriptionCapabilities: ({available: true})
        property var ttsProviderStatus: ({provider: "one", fallback: "none", providers: [
            {id: "one", name: "One", available: true},
            {id: "two", name: "Two", available: true}
        ]})
        property int saves: 0
        function loadSettingsStatus() {}
        function setTtsProviders(primary, fallback, unused) { saves++; }
    }

    Component {
        id: panelComponent
        Clarp.SettingsPanel {
            width: testCase.width
            height: testCase.height
            controller: stub
            visible: false
        }
    }
    SignalSpy { id: closeSpy; signalName: "closeRequested" }
    SignalSpy { id: connectionSpy; signalName: "openConnection" }

    function openPanel() {
        const panel = createTemporaryObject(panelComponent, testCase);
        verify(panel !== null);
        panel.visible = true;
        waitForRendering(panel);
        return panel;
    }

    function test_openNavigateToggleAndEscape() {
        stub.timestampsVisible = false;
        stub.toolsVisible = false;
        const panel = openPanel();
        const first = findChild(panel, "setting-timestamps");
        const second = findChild(panel, "setting-tools");
        verify(first !== null && second !== null);
        tryCompare(first, "activeFocus", true);
        keyClick(Qt.Key_Space);
        compare(stub.timestampsVisible, true);
        compare(first.activeFocus, true);
        keyClick(Qt.Key_Down);
        tryCompare(second, "activeFocus", true);
        keyClick(Qt.Key_Return);
        compare(stub.toolsVisible, true);
        keyClick(Qt.Key_Tab, Qt.ShiftModifier);
        tryCompare(first, "activeFocus", true);
        keyClick(Qt.Key_Tab);
        tryCompare(second, "activeFocus", true);
        closeSpy.target = panel;
        closeSpy.clear();
        keyClick(Qt.Key_Escape);
        compare(closeSpy.count, 1);
    }

    function test_lastRowScrollsIntoViewAndTabWraps() {
        const panel = openPanel();
        const first = findChild(panel, "setting-timestamps");
        const last = findChild(panel, "setting-voice-routing");
        verify(first !== null && last !== null);
        tryCompare(first, "activeFocus", true);
        keyClick(Qt.Key_End);
        tryCompare(last, "activeFocus", true);
        tryVerify(() => last.mapToItem(panel, 0, 0).y >= 0
            && last.mapToItem(panel, 0, last.height).y <= panel.height);
        keyClick(Qt.Key_Tab);
        tryCompare(first, "activeFocus", true);
        tryVerify(() => first.mapToItem(panel, 0, 0).y >= 0);
        const heading = findChild(panel, "settingsHeading");
        verify(heading !== null);
        verify(heading.mapToItem(panel, 0, 0).y >= 0,
               "Returning to the first setting must also reveal the title and keyboard hints");
    }

    function test_linksAndVoiceDialogRestoreFocusWithoutSaving() {
        stub.saves = 0;
        const panel = openPanel();
        const connection = findChild(panel, "setting-connection");
        const routing = findChild(panel, "setting-voice-routing");
        verify(connection !== null && routing !== null);
        connectionSpy.target = panel;
        connectionSpy.clear();
        connection.forceActiveFocus();
        keyClick(Qt.Key_Return);
        compare(connectionSpy.count, 1);
        routing.forceActiveFocus();
        keyClick(Qt.Key_Return);
        const primary = findChild(panel, "settingsPrimaryProvider");
        verify(primary !== null);
        tryCompare(primary, "activeFocus", true);
        verify(panel.dialogOpen);
        keyClick(Qt.Key_Space);
        tryCompare(primary.popup, "visible", true);
        keyClick(Qt.Key_Escape);
        tryCompare(primary.popup, "visible", false);
        verify(panel.dialogOpen, "Escape must close the provider list before closing its dialog");
        keyClick(Qt.Key_Escape);
        tryCompare(panel, "dialogOpen", false);
        tryCompare(routing, "activeFocus", true);
        compare(stub.saves, 0);
    }

    function test_navigationSkipsDisabledRowsInBothDirections() {
        const panel = openPanel();
        const first = findChild(panel, "setting-timestamps");
        const second = findChild(panel, "setting-tools");
        const third = findChild(panel, "setting-tool-narration");
        verify(first !== null && second !== null && third !== null);
        tryCompare(first, "activeFocus", true);
        second.enabled = false;
        keyClick(Qt.Key_Down);
        tryCompare(third, "activeFocus", true);
        keyClick(Qt.Key_Up);
        tryCompare(first, "activeFocus", true);
    }

    function test_narrowSettingsKeepsTheFocusedRowInsideTheViewport() {
        const panel = openPanel();
        panel.width = 320;
        const first = findChild(panel, "setting-timestamps");
        verify(first !== null);
        waitForRendering(panel);
        verify(first.mapToItem(panel, first.width, 0).x <= panel.width,
               "The focused row and ON/OFF value must not be clipped in a narrow window");
    }
}
