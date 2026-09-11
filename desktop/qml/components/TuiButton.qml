import QtQuick
import QtQuick.Controls
Button {
    id: control
    font.family: "JetBrains Mono"
    background: Rectangle {
        implicitWidth: 100
        implicitHeight: 40
        color: control.down ? Qt.darker(control.palette.button, 1.12) : control.hovered ? Qt.lighter(control.palette.button, 1.12) : control.palette.button
        border.width: 1
        border.color: control.visualFocus || control.checked ? control.palette.highlight : control.palette.midlight
    }
}
