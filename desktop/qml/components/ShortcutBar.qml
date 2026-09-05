import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller

    implicitHeight: 22
    color: "#11131a"
    border.color: "#262938"
    border.width: 1

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        spacing: 7

        Rectangle {
            implicitWidth: 6
            implicitHeight: 6
            radius: 3
            color: root.controller.connected ? "#8aaa7a" : "#8b7868"
        }

        Text {
            text: root.controller.panes.activeSession || "ready"
            color: "#8e92aa"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
            font.weight: Font.DemiBold
            elide: Text.ElideRight
        }

        Item {
            Layout.fillWidth: true
        }

        Text {
            text: root.controller.panes.zoomedPaneId.length > 0
                ? "ZOOM" : root.controller.panes.paneCount + "P"
            color: "#6e7289"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
        }

        Text {
            text: "⌃K"
            color: "#575b70"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
        }

    }

}
