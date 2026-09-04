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
    signal relaunchRequested(string session, string name)
    signal voiceRequested(string session, string name)
    signal orchestratorRequested

    color: "#f0121116"

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 28
        radius: 20
        color: "#17151c"
        border.color: "#37303f"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            RowLayout {
                Layout.fillWidth: true

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Text {
                        text: "AGENT OVERVIEW"
                        color: "#f1ebe6"
                        font.pixelSize: 22
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.2
                    }
                    Text {
                        text: root.controller.agents.count + " active conversations"
                        color: "#77717f"
                        font.pixelSize: 11
                    }
                }

                Button {
                    text: "New agent"
                    onClicked: root.startRequested("")
                }
                Button {
                    text: "Orchestrator"
                    onClicked: root.orchestratorRequested()
                }
                ToolButton {
                    text: "↻"
                    onClicked: root.controller.refreshAgents()
                }
                ToolButton {
                    text: "×"
                    onClicked: root.closeRequested()
                }
            }

            ListView {
                id: overviewList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 10
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

                    width: ListView.view.width
                    implicitHeight: cardColumn.implicitHeight + 24
                    radius: 15
                    color: root.controller.selectedSession === session ? "#26212c" : "#1d1a22"
                    border.color: root.controller.selectedSession === session ? "#594465" : "#2d2933"

                    ColumnLayout {
                        id: cardColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 12
                        spacing: 10

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12

                            Rectangle {
                                Layout.preferredWidth: 42
                                Layout.preferredHeight: 42
                                radius: 12
                                color: card.busy ? "#684d35" : "#3b3044"
                                Text {
                                    anchors.centerIn: parent
                                    text: card.name.slice(0, 1).toUpperCase()
                                    color: "#f1e9f4"
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    text: card.name
                                    color: "#eee8e2"
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: card.lastMessage || card.statusText || card.agentState
                                    color: "#837b89"
                                    font.pixelSize: 11
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
                                color: "#a698ad"
                                font.pixelSize: 10
                            }
                            Label {
                                text: card.modelName || "provider default"
                                color: "#77717f"
                                font.pixelSize: 10
                            }
                            Label {
                                Layout.fillWidth: true
                                text: card.workingDirectory
                                color: "#77717f"
                                font.pixelSize: 10
                                elide: Text.ElideMiddle
                            }
                            Label {
                                visible: card.queueCount > 0
                                text: card.queueCount + " queued"
                                color: "#dba162"
                                font.pixelSize: 10
                            }
                        }

                        ProgressBar {
                            visible: card.contextWindow > 0
                            Layout.fillWidth: true
                            from: 0
                            to: Math.max(1, card.contextWindow)
                            value: card.contextTokens
                        }

                        ColumnLayout {
                            visible: card.schedules.length > 0
                            Layout.fillWidth: true
                            spacing: 5

                            Label {
                                text: "SCHEDULED TASKS"
                                color: "#8d8492"
                                font.pixelSize: 9
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
                                                font.pixelSize: 10
                                                font.weight: Font.Medium
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: String(scheduleRow.modelData.prompt || "")
                                                color: "#716a77"
                                                font.pixelSize: 9
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
                            spacing: 7

                            Button {
                                text: "Open"
                                onClicked: {
                                    root.controller.selectSession(card.session);
                                    root.closeRequested();
                                }
                            }
                            Button {
                                text: "Relaunch"
                                onClicked: root.relaunchRequested(card.session, card.name)
                            }
                            Button {
                                text: "Voice"
                                onClicked: root.voiceRequested(card.session, card.name)
                            }
                            Button {
                                text: card.heartbeatEnabled ? "Heartbeat on" : "Heartbeat off"
                                onClicked: root.controller.setAgentHeartbeat(card.session, !card.heartbeatEnabled)
                            }
                            Button {
                                text: card.dreamingEnabled ? "Dreaming on" : "Dreaming off"
                                onClicked: root.controller.setAgentDreaming(card.session, !card.dreamingEnabled)
                            }
                            Button {
                                text: card.muted ? "Push muted" : "Push on"
                                onClicked: root.controller.setAgentPushMuted(card.session, !card.muted)
                            }
                            Item {
                                Layout.fillWidth: true
                            }
                            Button {
                                text: "Archive"
                                onClicked: root.controller.setAgentArchived(card.session, true)
                            }
                            Button {
                                text: root.confirmRelease === card.session ? "Confirm release" : "Release"
                                visible: card.name !== "Mike"
                                onClicked: {
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
                    font.pixelSize: 10
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
                        radius: 10
                        color: "#1b191f"
                        border.color: "#2d2933"

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
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: archivedCard.lastMessage || archivedCard.session
                                    color: "#6f6874"
                                    font.pixelSize: 9
                                    elide: Text.ElideRight
                                }
                            }
                            Button {
                                text: "Restore"
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
                    font.pixelSize: 10
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
                        width: 210
                        height: ListView.view.height
                        radius: 11
                        color: "#201c25"
                        border.color: "#312b38"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 9

                            Rectangle {
                                Layout.preferredWidth: 36
                                Layout.preferredHeight: 36
                                radius: 11
                                color: "#3b3045"
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
                                    font.pixelSize: 12
                                    font.weight: Font.Medium
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: contactCard.description || "Ready to start"
                                    color: "#746d7a"
                                    font.pixelSize: 9
                                    elide: Text.ElideRight
                                }
                            }
                            Button {
                                text: "Start"
                                onClicked: root.startRequested(contactCard.name)
                            }
                        }
                    }

                    ScrollBar.horizontal: ScrollBar {}
                }
            }
        }
    }
}
