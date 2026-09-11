import QtQuick
import QtQuick.Controls
import QtTest
import "../../qml/components"

TestCase {
    id: scene
    name: "TuiControlBehavior"
    visible: true
    when: windowShown
    width: 560
    height: 260

    TuiButton { id: action; text: "Apply"; width: 120; height: 40 }
    SignalSpy { id: clicks; target: action; signalName: "clicked" }
    TuiCheckBox { id: check; y: 50; text: "Enabled"; tristate: true }
    TuiSwitch { id: toggle; y: 90; text: "Notifications" }
    TuiRadioButton { id: first; x: 280; y: 50; text: "First" }
    TuiRadioButton { id: second; x: 280; y: 90; text: "Second" }
    TuiTextField { id: entry; y: 150; width: 240; placeholderText: "Name" }

    function test_keyboardActionsAndState() {
        action.forceActiveFocus(Qt.TabFocusReason);
        verify(action.visualFocus);
        clicks.clear();
        keyClick(Qt.Key_Space);
        compare(clicks.count, 1);
        check.checkState = Qt.Unchecked;
        check.forceActiveFocus(Qt.TabFocusReason);
        keyClick(Qt.Key_Space);
        compare(check.checkState, Qt.PartiallyChecked);
        keyClick(Qt.Key_Space);
        compare(check.checkState, Qt.Checked);
        toggle.checked = false;
        toggle.forceActiveFocus(Qt.TabFocusReason);
        keyClick(Qt.Key_Space);
        verify(toggle.checked);
        first.forceActiveFocus(Qt.TabFocusReason);
        keyClick(Qt.Key_Space);
        verify(first.checked);
        second.forceActiveFocus(Qt.TabFocusReason);
        keyClick(Qt.Key_Space);
        verify(second.checked && !first.checked);
        entry.forceActiveFocus();
        entry.text = "";
        keyClick(Qt.Key_A);
        compare(entry.text, "a");
    }
}
