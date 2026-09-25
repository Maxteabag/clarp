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

    color: Qt.alpha(Theme.sunken, 0.85)

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 18
        radius: 0
        color: Theme.raised
        border.color: Theme.border

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 10

            RowLayout {
                Layout.fillWidth: true

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    TuiText {
                        text: "AGENTS"
                        color: Theme.text
                        font.family: "JetBrains Mono"
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.5
                    }
                    TuiText {
                        text: root.controller.agents.count + " active conversations"
                        color: Theme.muted
                        font.family: "JetBrains Mono"
                        font.pixelSize: 11
                    }
                }

                TuiButton {
                    text: "+ New"
                    implicitWidth: 70
                    implicitHeight: 28
                    onClicked: root.startRequested("")
                }
                TuiToolButton {
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
                    radius: 0
                    color: root.controller.selectedSession === session ? Theme.control : Theme.raised
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

                            AvatarActivity {
                                id: overviewAvatarActivity
                                working: card.busy
                                Layout.preferredWidth: 34
                                Layout.preferredHeight: 34
                                controller: root.controller
                                session: card.session
                                showPortrait: true
                                name: card.name
                                symbol: card.avatarSymbol
                                avatarSize: 34
                                cornerRadius: 8
                                fallbackColor: card.busy ? Theme.faint : Theme.border
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                TuiText {
                                    text: card.name
                                    color: Theme.text
                                    font.family: "JetBrains Mono"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                }
                                TuiText {
                                    Layout.fillWidth: true
                                    text: card.lastMessage || card.statusText || card.agentState
                                    ActivitySweep { anchors.fill: parent; working: overviewAvatarActivity.authoritativeWorking; reducedMotion: overviewAvatarActivity.reducedMotion; phase: overviewAvatarActivity.phase }
                                    color: Theme.muted
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

                            TuiLabel {
                                text: card.backend
                                color: Theme.secondary
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                            TuiLabel {
                                text: card.modelName || "provider default"
                                color: Theme.faint
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                            TuiLabel {
                                Layout.fillWidth: true
                                text: card.workingDirectory
                                color: Theme.faint
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                            }
                            TuiLabel {
                                visible: card.queueCount > 0
                                text: card.queueCount + " queued"
                                color: Theme.secondary
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
                                color: Theme.border
                                radius: 0
                            }
                            contentItem: Item {
                                implicitHeight: 3
                                Rectangle {
                                    width: contextProgress.visualPosition * parent.width
                                    height: parent.height
                                    radius: 0
                                    color: Theme.muted
                                }
                            }
                        }

                        ColumnLayout {
                            visible: card.schedules.length > 0
                            Layout.fillWidth: true
                            spacing: 5

                            TuiLabel {
                                text: "SCHEDULED TASKS"
                                color: Theme.muted
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
                                    radius: 0
                                    color: Theme.window
                                    border.color: Theme.control

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 10
                                        anchors.rightMargin: 8
                                        spacing: 10

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 1
                                            TuiText {
                                                Layout.fillWidth: true
                                                text: String(scheduleRow.modelData.name || "Scheduled task") + "  ·  " + String(scheduleRow.modelData.cron_expression || "")
                                                color: Theme.text
                                                font.pixelSize: 12
                                                font.weight: Font.Medium
                                                elide: Text.ElideRight
                                            }
                                            TuiText {
                                                Layout.fillWidth: true
                                                text: String(scheduleRow.modelData.prompt || "")
                                                color: Theme.muted
                                                font.pixelSize: 11
                                                elide: Text.ElideRight
                                            }
                                        }
                                        TuiButton {
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

                            TuiButton {
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
                            TuiToolButton {
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

                TuiText {
                    text: "ARCHIVED"
                    color: Theme.muted
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
                    radius: 0
                    color: Theme.raised
                    border.color: Theme.border

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 9
                            spacing: 8
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                TuiText {
                                    text: archivedCard.name
                                    color: Theme.text
                                    font.pixelSize: 13
                                    font.weight: Font.Medium
                                }
                                TuiText {
                                    Layout.fillWidth: true
                                    text: archivedCard.lastMessage || archivedCard.session
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                            }
                            TuiButton {
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

                TuiText {
                    text: "READY CONTACTS"
                    color: Theme.muted
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
                        radius: 0
                        color: Theme.raised
                        border.color: Theme.border

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 9

                            Rectangle {
                                Layout.preferredWidth: 36
                                Layout.preferredHeight: 36
                                radius: 0
                                color: Theme.border
                                TuiText {
                                    anchors.centerIn: parent
                                    text: contactCard.avatarSymbol || contactCard.name.slice(0, 1).toUpperCase()
                                    color: Theme.text
                                    font.weight: Font.DemiBold
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                TuiText {
                                    text: contactCard.name
                                    color: Theme.text
                                    font.pixelSize: 14
                                    font.weight: Font.Medium
                                }
                                TuiText {
                                    Layout.fillWidth: true
                                    text: root.controller.quickStartBackend() + " · " + root.controller.lastWorkingDirectory
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                            }
                            TuiButton {
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
