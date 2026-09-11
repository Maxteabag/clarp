import QtQuick
import QtQuick.Controls
TextField {
    id: control
    font.family: "JetBrains Mono"
    background: Rectangle {
        implicitWidth: 200
        implicitHeight: 40
        color: control.palette.base
        border.width: 1
        border.color: control.activeFocus ? control.palette.highlight : control.palette.midlight
    }
}
