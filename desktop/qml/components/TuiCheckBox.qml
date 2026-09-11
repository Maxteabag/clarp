import QtQuick
import QtQuick.Controls
CheckBox {
    id: control
    font.family: "JetBrains Mono"
    indicator: Item {
        implicitWidth: 28
        implicitHeight: 26
        x: control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        Text {
        anchors.fill: parent
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        text: control.checkState === Qt.PartiallyChecked ? "[-]" : control.checked ? "[x]" : "[ ]"
        font: control.font
        color: control.visualFocus ? control.palette.highlight : control.palette.text
        }
    }
}
