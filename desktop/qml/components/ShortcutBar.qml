import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller

    implicitHeight: 29
    color: "#101015"
    border.color: "#29242f"
    border.width: 1

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 12

        Text {
            text: root.controller.connected ? "LIVE" : root.controller.connectionState.toUpperCase()
            color: root.controller.connected ? "#70bc98" : "#b9996f"
            font.pixelSize: 9
            font.weight: Font.Bold
            font.letterSpacing: 1
        }

        Text {
            Layout.fillWidth: true
            text: "Ctrl+K  Switch   ·   Ctrl+N  New   ·   Ctrl+Shift+V/H  Split   ·   Ctrl+Alt+Arrows  Move   ·   Ctrl+Shift+Z  Zoom"
            color: "#716a78"
            font.pixelSize: 9
            elide: Text.ElideRight
        }

        Text {
            text: "Qt 6 · native"
            color: "#56515b"
            font.pixelSize: 9
        }

    }

}
