pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property bool collapsed: true
    property string selectedSurface: "chats"
    signal openOverview
    signal selectSurface(string surface)

    color: "#171821"
    border.color: "#292b3a"
    border.width: 0

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 42

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 11
                anchors.rightMargin: 7
                spacing: 8

                Rectangle {
                    Layout.preferredWidth: 18
                    Layout.preferredHeight: 18
                    radius: 4
                    color: "transparent"
                    border.color: "#3b3d50"

                    Text {
                        anchors.centerIn: parent
                        text: "C"
                        color: "#9296b1"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }

                ColumnLayout {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                        text: "CLARP"
                        color: "#b9bcd0"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.7
                    }
                    Text {
                        text: root.controller.serverName || "desktop"
                        color: "#585b70"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 9
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                ToolButton {
                    visible: !root.collapsed
                    text: "‹"
                    implicitWidth: 22
                    implicitHeight: 22
                    font.pixelSize: 16
                    onClicked: root.collapsed = true
                    ToolTip.visible: hovered
                    ToolTip.text: "Collapse conversations"
                }
            }

            TapHandler {
                enabled: root.collapsed
                onTapped: root.collapsed = false
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: "#27232d"
        }

        ListView {
            id: agentList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: root.controller.agents
            spacing: 3
            topMargin: 8
            bottomMargin: 8
            reuseItems: true

            delegate: ItemDelegate {
                id: agentDelegate

                required property string session
                required property string name
                required property string backend
                required property string lastMessage
                required property string agentState
                required property bool busy
                required property bool unread
                required property bool focused
                required property int queueCount
                required property string avatarUrl
                required property string avatarSymbol

                width: ListView.view.width
                height: 46
                leftPadding: 7
                rightPadding: 7
                hoverEnabled: true
                highlighted: root.controller.selectedSession === session
                onClicked: {
                    root.selectSurface("chats");
                    root.controller.selectSession(session);
                    root.controller.requestComposerFocus(
                        root.controller.panes.activePaneId);
                }

                background: Rectangle {
                    radius: 4
                    color: agentDelegate.highlighted ? "#242634" : agentDelegate.hovered ? "#20212d" : "transparent"
                    border.color: "transparent"

                    Rectangle {
                        visible: agentDelegate.highlighted
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        width: 2
                        height: 20
                        radius: 1
                        color: "#999db7"
                    }
                }

                contentItem: RowLayout {
                    spacing: 8

                    Item {
                        Layout.preferredWidth: 30
                        Layout.preferredHeight: 30

                        AgentAvatar {
                            anchors.fill: parent
                            controller: root.controller
                            session: agentDelegate.session
                            name: agentDelegate.name
                            symbol: agentDelegate.avatarSymbol
                            avatarSize: 30
                            cornerRadius: 7
                            fallbackColor: agentDelegate.highlighted ? "#555970" : "#3b3e50"
                        }

                        Rectangle {
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            visible: agentDelegate.unread || agentDelegate.busy
                            width: 8
                            height: 8
                            radius: 4
                            color: agentDelegate.unread ? "#c27d90" : "#91a67f"
                            border.color: "#171821"
                            border.width: 2
                        }
                    }

                    ColumnLayout {
                        visible: !root.collapsed
                        Layout.fillWidth: true
                        spacing: 2

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            Text {
                                Layout.fillWidth: true
                                text: agentDelegate.name
                                color: "#c6c8d9"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 12
                                font.weight: agentDelegate.unread ? Font.Bold : Font.Medium
                                elide: Text.ElideRight
                            }
                            Text {
                                visible: agentDelegate.queueCount > 0
                                text: agentDelegate.queueCount
                                color: "#a7a1b8"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 10
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            visible: agentDelegate.busy || agentDelegate.unread
                            text: agentDelegate.busy ? (agentDelegate.agentState || "working") : "new reply"
                            color: agentDelegate.busy ? "#8fa180" : "#9b8290"
                            font.family: "JetBrains Mono"
                            font.pixelSize: 9
                            elide: Text.ElideRight
                            maximumLineCount: 1
                        }
                    }
                }
            }

            ScrollBar.vertical: ScrollBar {}
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: "#292b3a"
        }

        ItemDelegate {
            id: chatsButton
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            highlighted: root.selectedSurface === "chats"
            onClicked: root.selectSurface("chats")
            contentItem: RowLayout {
                spacing: 8
                Text {
                    Layout.preferredWidth: root.collapsed ? 32 : 18
                    text: "◌"
                    color: chatsButton.highlighted ? "#afb5dc" : "#696d84"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 13
                }
                Text {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    text: "Chats"
                    color: chatsButton.highlighted ? "#c7cadc" : "#74788f"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 10
                }
            }
            background: Rectangle {
                color: chatsButton.highlighted ? "#24283a" : "transparent"
            }
        }

        ItemDelegate {
            id: updatesButton
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            highlighted: root.selectedSurface === "updates"
            onClicked: root.selectSurface("updates")
            contentItem: RowLayout {
                spacing: 8
                Text {
                    Layout.preferredWidth: root.collapsed ? 32 : 18
                    text: "◇"
                    color: updatesButton.highlighted ? "#afb5dc" : "#696d84"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 13
                }
                Text {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    text: "Updates"
                    color: updatesButton.highlighted ? "#c7cadc" : "#74788f"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 10
                }
                Rectangle {
                    visible: root.controller.attentionCount > 0
                    Layout.preferredWidth: 18
                    Layout.preferredHeight: 16
                    radius: 8
                    color: "#aeb4dc"
                    Text {
                        anchors.centerIn: parent
                        text: String(root.controller.attentionCount)
                        color: "#171923"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 9
                    }
                }
            }
            background: Rectangle {
                color: updatesButton.highlighted ? "#24283a" : "transparent"
            }
        }

        ItemDelegate {
            id: teamsButton
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            highlighted: root.selectedSurface === "teams"
            onClicked: root.selectSurface("teams")
            contentItem: RowLayout {
                spacing: 8
                Text {
                    Layout.preferredWidth: root.collapsed ? 32 : 18
                    text: "△"
                    color: teamsButton.highlighted ? "#afb5dc" : "#696d84"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 13
                }
                Text {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    text: "Teams"
                    color: teamsButton.highlighted ? "#c7cadc" : "#74788f"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 10
                }
            }
            background: Rectangle {
                color: teamsButton.highlighted ? "#24283a" : "transparent"
            }
        }

        ItemDelegate {
            id: allAgentsButton
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            text: root.collapsed ? "≡" : "All agents"
            leftPadding: root.collapsed ? 0 : 11
            onClicked: root.openOverview()
            contentItem: Text {
                text: allAgentsButton.text
                color: "#686b81"
                font.family: "JetBrains Mono"
                font.pixelSize: 10
                font.weight: Font.Medium
                horizontalAlignment: root.collapsed ? Text.AlignHCenter : Text.AlignLeft
                verticalAlignment: Text.AlignVCenter
            }
        }

        ItemDelegate {
            id: settingsButton
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            highlighted: root.selectedSurface === "settings"
            onClicked: root.selectSurface("settings")
            contentItem: RowLayout {
                spacing: 8
                Text {
                    Layout.preferredWidth: root.collapsed ? 32 : 18
                    text: "⚙"
                    color: settingsButton.highlighted ? "#afb5dc" : "#696d84"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 12
                }
                Text {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    text: "Settings"
                    color: settingsButton.highlighted ? "#c7cadc" : "#74788f"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 10
                }
            }
            background: Rectangle {
                color: settingsButton.highlighted ? "#24283a" : "transparent"
            }
        }
    }
}
