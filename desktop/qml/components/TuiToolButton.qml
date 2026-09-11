import QtQuick
import QtQuick.Controls
ToolButton {
    id: control
    font.family: "JetBrains Mono"
    background: Rectangle {
        implicitWidth: 40
        implicitHeight: 40
        color: control.down ? control.palette.midlight : control.hovered || control.checked ? control.palette.alternateBase : "transparent"
        border.width: control.visualFocus || control.checked ? 1 : 0
        border.color: control.palette.highlight
    }
}
