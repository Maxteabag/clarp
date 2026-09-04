import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller

    implicitHeight: 25
    color: "#15161e"
    border.color: "#2a2c3b"
    border.width: 1

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 9

        Rectangle {
            implicitWidth: 5
            implicitHeight: 5
            radius: 2.5
            color: root.controller.connected ? "#829b73" : "#817161"
        }

        Text {
            text: root.controller.panes.zoomedPaneId.length > 0 ? "ZOOM" : "PANE"
            color: "#b3b6cb"
            font.family: "JetBrains Mono"
            font.pixelSize: 8
            font.weight: Font.DemiBold
            font.letterSpacing: 0.8
        }

        Text {
            Layout.fillWidth: true
            text: "Ctrl+K commands    Ctrl+Alt+arrows move    Ctrl+Alt+Z zoom    Ctrl+N new"
            color: "#64677d"
            font.family: "JetBrains Mono"
            font.pixelSize: 8
            elide: Text.ElideRight
        }

        Text {
            text: root.controller.panes.paneCount + (root.controller.panes.paneCount === 1 ? " pane" : " panes")
            color: "#55586d"
            font.family: "JetBrains Mono"
            font.pixelSize: 8
        }

    }

}
