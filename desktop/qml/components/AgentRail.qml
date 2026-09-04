pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property bool collapsed: false
    signal openOverview

    color: "#17151c"
    border.color: "#292530"
    border.width: 0

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 68

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 12
                spacing: 10

                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    radius: 9
                    gradient: Gradient {
                        GradientStop {
                            position: 0
                            color: "#e9ae78"
                        }
                        GradientStop {
                            position: 1
                            color: "#a875d5"
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        text: "C"
                        color: "#17121b"
                        font.pixelSize: 16
                        font.weight: Font.Bold
                    }
                }

                ColumnLayout {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                        text: "CLARP"
                        color: "#f1ece6"
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        font.letterSpacing: 2
                    }
                    Text {
                        text: root.controller.serverName || "Native desktop"
                        color: "#756e7c"
                        font.pixelSize: 10
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                ToolButton {
                    visible: !root.collapsed
                    text: "‹"
                    font.pixelSize: 22
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

                width: ListView.view.width
                height: 64
                leftPadding: 12
                rightPadding: 12
                hoverEnabled: true
                highlighted: root.controller.selectedSession === session
                onClicked: root.controller.selectSession(session)

                background: Rectangle {
                    radius: 11
                    color: agentDelegate.highlighted ? "#292330" : agentDelegate.hovered ? "#211e27" : "transparent"
                    border.color: agentDelegate.highlighted ? "#55415f" : "transparent"
                }

                contentItem: RowLayout {
                    spacing: 11

                    Item {
                        Layout.preferredWidth: 40
                        Layout.preferredHeight: 40

                        Rectangle {
                            anchors.fill: parent
                            radius: 12
                            color: agentDelegate.highlighted ? "#6f527b" : "#312b39"

                            Text {
                                anchors.centerIn: parent
                                text: agentDelegate.name.slice(0, 1).toUpperCase()
                                color: "#f3ecf5"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                            }
                        }

                        Rectangle {
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            width: 10
                            height: 10
                            radius: 5
                            color: agentDelegate.unread ? "#df7676" : agentDelegate.busy ? "#e9aa67" : "#6db895"
                            border.color: "#17151c"
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
                                color: "#eee8e2"
                                font.pixelSize: 13
                                font.weight: agentDelegate.unread ? Font.Bold : Font.Medium
                                elide: Text.ElideRight
                            }
                            Text {
                                visible: agentDelegate.queueCount > 0
                                text: agentDelegate.queueCount
                                color: "#e7ae73"
                                font.pixelSize: 10
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: agentDelegate.busy ? (agentDelegate.agentState || "working") : (agentDelegate.lastMessage || agentDelegate.backend)
                            color: agentDelegate.busy ? "#dba163" : "#77717f"
                            font.pixelSize: 11
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
            color: "#27232d"
        }

        ItemDelegate {
            id: allAgentsButton
            Layout.fillWidth: true
            Layout.preferredHeight: 54
            text: root.collapsed ? "◎" : "All agents"
            leftPadding: root.collapsed ? 0 : 20
            onClicked: root.openOverview()
            contentItem: Text {
                text: allAgentsButton.text
                color: "#a49dab"
                font.pixelSize: 12
                font.weight: Font.Medium
                horizontalAlignment: root.collapsed ? Text.AlignHCenter : Text.AlignLeft
                verticalAlignment: Text.AlignVCenter
            }
        }
    }
}
