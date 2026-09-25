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
    color: Theme.sunken

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: Theme.rule
    }

    component RailButton: TuiToolButton {
        id: railButton

        property int badge: 0
        property bool selected: false
        property string iconName: ""

        implicitWidth: 42
        implicitHeight: 42
        Layout.alignment: Qt.AlignHCenter
        ToolTip.visible: hovered
        ToolTip.delay: 400
        display: AbstractButton.IconOnly
        icon.source: iconName.length > 0 ? Qt.resolvedUrl("../../resources/icons/rail-" + iconName + ".svg") : ""
        icon.width: 20
        icon.height: 20
        icon.color: railButton.selected ? Theme.text : Theme.muted

        background: Rectangle {
            radius: 10
            color: railButton.selected ? Theme.control : railButton.hovered ? Theme.raised : "transparent"
        }

        // A count pill in the top-right corner; always round, whatever the theme.
        Rectangle {
            visible: railButton.badge > 0
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.rightMargin: -2
            anchors.topMargin: -2
            implicitWidth: Math.max(16, badgeLabel.implicitWidth + 8)
            implicitHeight: 16
            radius: 8
            color: Theme.accent
            border.color: Theme.sunken
            border.width: 1.5

            TuiText {
                id: badgeLabel

                anchors.centerIn: parent
                text: railButton.badge
                color: Theme.accentText
                font.pixelSize: 9
                font.weight: Font.Bold
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: 12
        anchors.bottomMargin: 12
        spacing: 4

        RailButton {
            iconName: "chats"
            selected: root.selectedSurface === "chats"
            onClicked: root.selectSurface("chats")
            ToolTip.text: "Chats"
        }

        RailButton {
            iconName: "overview"
            onClicked: root.openOverview()
            ToolTip.text: "All agents (Ctrl+Shift+O)"
        }

        RailButton {
            iconName: "search"
            onClicked: root.openSwitcher()
            ToolTip.text: "Go to agent (Ctrl+K)"
        }

        RailButton { iconName: "updates"; badge: root.controller.attentionCount; selected: root.selectedSurface === "updates"; onClicked: root.selectSurface("updates"); ToolTip.text: "Updates (Ctrl+2)" }
        RailButton { iconName: "teams"; selected: root.selectedSurface === "teams"; onClicked: root.selectSurface("teams"); ToolTip.text: "Teams (Ctrl+3)" }

        Item {
            Layout.fillHeight: true
        }

        RailButton {
            iconName: root.controller.muted ? "muted" : "sound"
            onClicked: root.controller.muted = !root.controller.muted
            ToolTip.text: root.controller.muted ? "Voice replies muted (Ctrl+M)" : "Voice replies on (Ctrl+M)"
        }

        RailButton {
            iconName: "settings"
            onClicked: root.openConnection()
            ToolTip.text: "Settings (Ctrl+,)"
            selected: root.selectedSurface === "settings"
        }

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 4
            implicitWidth: 30
            implicitHeight: 30
            radius: 15
            color: root.controller.connected ? Qt.alpha(Theme.success, 0.16) : Qt.alpha(Theme.warning, 0.22)
            border.color: root.controller.connected ? Theme.success : Theme.warning
            border.width: 1

            TuiText {
                anchors.centerIn: parent
                text: root.controller.serverName.length > 0 ? root.controller.serverName.slice(0, 1).toUpperCase() : "?"
                color: root.controller.connected ? Theme.success : Theme.warning
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
