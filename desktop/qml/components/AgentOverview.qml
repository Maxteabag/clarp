pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string confirmRelease: ""
    signal closeRequested
    signal startRequested(string name)
    signal quickStartRequested(string name)
    signal relaunchRequested(string session, string name)
    signal voiceRequested(string session, string name)
    signal orchestratorRequested

    color: "#d908090f"

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 18
        radius: 8
        color: "#171821"
        border.color: "#343648"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 10

            RowLayout {
                Layout.fillWidth: true

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Text {
                        text: "AGENTS"
                        color: "#c8cadc"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 19
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.5
                    }
                    Text {
                        text: root.controller.agents.count + " active conversations"
                        color: "#62657a"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 11
                    }
                }

                Button {
                    text: "+ New"
                    implicitWidth: 70
                    implicitHeight: 28
                    onClicked: root.startRequested("")
                }
                ToolButton {
                    text: "×"
                    implicitWidth: 28
                    implicitHeight: 28
                    onClicked: root.closeRequested()
                }
            }

            ListView {
                id: overviewList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 6
                model: root.controller.agents
                reuseItems: true

                delegate: Rectangle {
                    id: card

                    required property string session
                    required property string name
                    required property string backend
                    required property string workingDirectory
                    required property string modelName
                    required property string effort
                    required property string lastMessage
                    required property string agentState
                    required property string statusText
                    required property bool busy
                    required property bool muted
                    required property bool heartbeatEnabled
                    required property bool dreamingEnabled
                    required property int queueCount
                    required property real contextTokens
                    required property real contextWindow
                    required property var schedules
                    required property string avatarSymbol

                    width: ListView.view.width
                    implicitHeight: cardColumn.implicitHeight + 18
                    radius: 4
                    color: root.controller.selectedSession === session ? "#242634" : "#1c1d28"
                    border.width: 0

                    ColumnLayout {
                        id: cardColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 9
                        spacing: 7

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 9

                            AgentAvatar {
                                Layout.preferredWidth: 34
                                Layout.preferredHeight: 34
                                controller: root.controller
                                session: card.session
                                name: card.name
                                symbol: card.avatarSymbol
                                avatarSize: 34
                                cornerRadius: 8
                                fallbackColor: card.busy ? "#5c5149" : "#414458"
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    text: card.name
                                    color: "#c8cadc"
                                    font.family: "JetBrains Mono"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: card.lastMessage || card.statusText || card.agentState
                                    color: "#686b80"
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                            }

                            StatusPill {
                                status: card.agentState
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Label {
                                text: card.backend
                                color: "#8e91aa"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                            Label {
                                text: card.modelName || "provider default"
                                color: "#5f6277"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                            Label {
                                Layout.fillWidth: true
                                text: card.workingDirectory
                                color: "#5f6277"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                            }
                            Label {
                                visible: card.queueCount > 0
                                text: card.queueCount + " queued"
                                color: "#9d91a7"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                        }

                        ProgressBar {
                            id: contextProgress
                            visible: card.contextWindow > 0
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 3 : 0
                            from: 0
                            to: Math.max(1, card.contextWindow)
                            value: card.contextTokens
                            background: Rectangle {
                                color: "#292b39"
                                radius: 1.5
                            }
                            contentItem: Item {
                                implicitHeight: 3
                                Rectangle {
                                    width: contextProgress.visualPosition * parent.width
                                    height: parent.height
                                    radius: 1.5
                                    color: "#666a83"
                                }
                            }
                        }

                        ColumnLayout {
                            visible: card.schedules.length > 0
                            Layout.fillWidth: true
                            spacing: 5

                            Label {
                                text: "SCHEDULED TASKS"
                                color: "#8d8492"
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                                font.letterSpacing: 1
                            }

                            Repeater {
                                model: card.schedules

                                Rectangle {
                                    id: scheduleRow
                                    required property var modelData
                                    Layout.fillWidth: true
                                    implicitHeight: 48
                                    radius: 9
                                    color: "#18161c"
                                    border.color: "#2c2831"

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 10
                                        anchors.rightMargin: 8
                                        spacing: 10

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 1
                                            Text {
                                                Layout.fillWidth: true
                                                text: String(scheduleRow.modelData.name || "Scheduled task") + "  ·  " + String(scheduleRow.modelData.cron_expression || "")
                                                color: "#cfc6d2"
                                                font.pixelSize: 12
                                                font.weight: Font.Medium
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: String(scheduleRow.modelData.prompt || "")
                                                color: "#716a77"
                                                font.pixelSize: 11
                                                elide: Text.ElideRight
                                            }
                                        }
                                        Button {
                                            text: Boolean(scheduleRow.modelData.enabled) ? "On" : "Off"
                                            checked: Boolean(scheduleRow.modelData.enabled)
                                            checkable: true
                                            onClicked: root.controller.setScheduleEnabled(String(scheduleRow.modelData.schedule_id), !Boolean(scheduleRow.modelData.enabled))
                                        }
                                    }
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 5

                            Button {
                                text: "Open"
                                implicitWidth: 66
                                implicitHeight: 28
                                onClicked: {
                                    root.controller.selectSession(card.session);
                                    root.closeRequested();
                                }
                            }
                            Item {
                                Layout.fillWidth: true
                            }
                            ToolButton {
                                text: "···"
                                implicitWidth: 28
                                implicitHeight: 28
                                onClicked: agentMenu.open()

                                Menu {
                                    id: agentMenu
                                    MenuItem { text: "Relaunch"; onTriggered: root.relaunchRequested(card.session, card.name) }
                                    MenuItem { text: "Voice"; onTriggered: root.voiceRequested(card.session, card.name) }
                                    MenuSeparator {}
                                    MenuItem {
                                        text: card.heartbeatEnabled ? "Disable heartbeat" : "Enable heartbeat"
                                        onTriggered: root.controller.setAgentHeartbeat(card.session, !card.heartbeatEnabled)
                                    }
                                    MenuItem {
                                        text: card.dreamingEnabled ? "Disable dreaming" : "Enable dreaming"
                                        onTriggered: root.controller.setAgentDreaming(card.session, !card.dreamingEnabled)
                                    }
                                    MenuItem {
                                        text: card.muted ? "Enable push alerts" : "Mute push alerts"
                                        onTriggered: root.controller.setAgentPushMuted(card.session, !card.muted)
                                    }
                                    MenuSeparator {}
                                    MenuItem {
                                        text: "Archive"
                                        onTriggered: root.controller.setAgentArchived(card.session, true)
                                    }
                                    MenuItem {
                                        visible: card.name !== "Mike"
                                        text: root.confirmRelease === card.session ? "Confirm release" : "Release…"
                                        onTriggered: {
                                            if (root.confirmRelease === card.session) {
                                                root.controller.releaseAgent(card.session);
                                                root.confirmRelease = "";
                                            } else {
                                                root.confirmRelease = card.session;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                ScrollBar.vertical: ScrollBar {}
            }

            ColumnLayout {
                visible: root.controller.archivedAgents.count > 0
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 88 : 0
                Layout.minimumHeight: visible ? 88 : 0
                Layout.maximumHeight: visible ? 88 : 0
                spacing: 6

                Text {
                    text: "ARCHIVED"
                    color: "#8d8492"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.2
                }

                ListView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 62
                    orientation: ListView.Horizontal
                    spacing: 8
                    clip: true
                    model: root.controller.archivedAgents

                    delegate: Rectangle {
                        id: archivedCard
                        required property string session
                        required property string name
                        required property string lastMessage
                        width: 250
                        height: ListView.view.height
                    radius: 4
                    color: "#1c1d28"
                    border.color: "#303142"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 9
                            spacing: 8
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                Text {
                                    text: archivedCard.name
                                    color: "#c9c1cb"
                                    font.pixelSize: 13
                                    font.weight: Font.Medium
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: archivedCard.lastMessage || archivedCard.session
                                    color: "#6f6874"
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                            }
                            Button {
                                text: "Restore"
                                implicitWidth: 68
                                implicitHeight: 28
                                onClicked: root.controller.setAgentArchived(archivedCard.session, false)
                            }
                        }
                    }
                }
            }

            ColumnLayout {
                visible: root.controller.contacts.count > 0
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 126 : 0
                Layout.minimumHeight: visible ? 126 : 0
                Layout.maximumHeight: visible ? 126 : 0
                spacing: 7

                Text {
                    text: "READY CONTACTS"
                    color: "#8d8492"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.2
                }

                ListView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 98
                    orientation: ListView.Horizontal
                    spacing: 8
                    clip: true
                    model: root.controller.contacts

                    delegate: Rectangle {
                        id: contactCard
                        required property string name
                        required property string description
                        required property string avatarSymbol
                        width: 280
                        height: ListView.view.height
                        radius: 4
                        color: "#1c1d28"
                        border.color: "#303142"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 9

                            Rectangle {
                                Layout.preferredWidth: 36
                                Layout.preferredHeight: 36
                                radius: 7
                                color: "#414458"
                                Text {
                                    anchors.centerIn: parent
                                    text: contactCard.avatarSymbol || contactCard.name.slice(0, 1).toUpperCase()
                                    color: "#f1e9f4"
                                    font.weight: Font.DemiBold
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                Text {
                                    text: contactCard.name
                                    color: "#e7dfe9"
                                    font.pixelSize: 14
                                    font.weight: Font.Medium
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.controller.quickStartBackend() + " · " + root.controller.lastWorkingDirectory
                                    color: "#746d7a"
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                            }
                            Button {
                                text: root.controller.startingContact === contactCard.name ? "Starting…" : "Start"
                                enabled: root.controller.startingContact.length === 0
                                implicitWidth: 62
                                implicitHeight: 28
                                onClicked: root.quickStartRequested(contactCard.name)
                            }
                        }
                    }

                    ScrollBar.horizontal: ScrollBar {}
                }
            }
        }
    }
}
