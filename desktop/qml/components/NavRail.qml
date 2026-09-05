import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openOverview
    signal openConnection
    signal openSwitcher
    signal selectSurface(string surface)
    property string selectedSurface: "chats"

    implicitWidth: 54
    color: "#101015"

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: "#221f29"
    }

    component RailButton: ToolButton {
        id: railButton

        property int badge: 0
        property bool selected: false

        implicitWidth: 42
        implicitHeight: 42
        Layout.alignment: Qt.AlignHCenter
        ToolTip.visible: hovered
        ToolTip.delay: 400

        background: Rectangle {
            radius: 12
            color: railButton.selected ? "#292b3a" : railButton.hovered ? "#1d1a23" : "transparent"
        }
        contentItem: Item {
            Text {
                anchors.centerIn: parent
                text: railButton.text
                color: railButton.selected ? "#dcc7ea" : "#8d93b0"
                font.pixelSize: 17
            }

            Rectangle {
                visible: railButton.badge > 0
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.rightMargin: -1
                anchors.topMargin: 1
                implicitWidth: Math.max(15, badgeLabel.implicitWidth + 8)
                implicitHeight: 15
                radius: height / 2
                color: "#bb9af7"
                border.color: "#101015"
                border.width: 1.5

                Text {
                    id: badgeLabel

                    anchors.centerIn: parent
                    text: railButton.badge
                    color: "#1a1b26"
                    font.pixelSize: 9
                    font.weight: Font.Bold
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: 12
        anchors.bottomMargin: 12
        spacing: 4

        RailButton {
            text: "▤"
            selected: root.selectedSurface === "chats"
            onClicked: root.selectSurface("chats")
            ToolTip.text: "Chats"
        }

        RailButton {
            text: "◎"
            onClicked: root.openOverview()
            ToolTip.text: "All agents (Ctrl+Shift+O)"
        }

        RailButton {
            text: "⌕"
            onClicked: root.openSwitcher()
            ToolTip.text: "Go to agent (Ctrl+K)"
        }

        RailButton { text: "◇"; badge: root.controller.attentionCount; selected: root.selectedSurface === "updates"; onClicked: root.selectSurface("updates"); ToolTip.text: "Updates (Ctrl+2)" }
        RailButton { text: "△"; selected: root.selectedSurface === "teams"; onClicked: root.selectSurface("teams"); ToolTip.text: "Teams (Ctrl+3)" }

        Item {
            Layout.fillHeight: true
        }

        RailButton {
            text: root.controller.muted ? "⃠" : "♪"
            onClicked: root.controller.muted = !root.controller.muted
            ToolTip.text: root.controller.muted ? "Voice replies muted (Ctrl+M)" : "Voice replies on (Ctrl+M)"
        }

        RailButton {
            text: "⚙"
            onClicked: root.openConnection()
            ToolTip.text: "Settings (Ctrl+,)"
            selected: root.selectedSurface === "settings"
        }

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 4
            implicitWidth: 30
            implicitHeight: 30
            radius: width / 2
            color: root.controller.connected ? "#1f3a2e" : "#3a2f1f"
            border.color: root.controller.connected ? "#6db895" : "#b9996f"
            border.width: 1

            Text {
                anchors.centerIn: parent
                text: root.controller.serverName.length > 0 ? root.controller.serverName.slice(0, 1).toUpperCase() : "?"
                color: root.controller.connected ? "#8fd4b3" : "#d3b98c"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }

            HoverHandler {
                id: serverHover
            }

            ToolTip.visible: serverHover.hovered
            ToolTip.text: (root.controller.serverName.length > 0 ? root.controller.serverName : root.controller.baseUrl) + " · " + root.controller.connectionState
        }
    }
}
