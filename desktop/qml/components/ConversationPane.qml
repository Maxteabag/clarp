pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    required property var conversationModel
    readonly property int agentRevision: controller.agentRevision
    signal openConnection

    color: "#121116"

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 66
            color: "#151319"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 20
                anchors.rightMargin: 18
                spacing: 11

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1

                    Text {
                        text: {
                            root.agentRevision;
                            return root.controller.agentName(root.session) || "No agent selected";
                        }
                        color: "#f0ebe6"
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: {
                            root.agentRevision;
                            return root.session.length > 0 ? root.controller.agentBackend(root.session) + "  ·  " + root.session : root.controller.baseUrl;
                        }
                        color: "#77717f"
                        font.pixelSize: 10
                    }
                }

                StatusPill {
                    status: {
                        root.agentRevision;
                        return root.controller.agentState(root.session) || root.controller.connectionState;
                    }
                }

                ToolButton {
                    text: "<>"
                    highlighted: root.controller.toolsVisible
                    onClicked: root.controller.toolsVisible = !root.controller.toolsVisible
                    ToolTip.visible: hovered
                    ToolTip.text: "Show or hide tool calls"
                }

                ToolButton {
                    text: "↻"
                    onClicked: root.controller.refreshSession(root.session)
                    ToolTip.visible: hovered
                    ToolTip.text: "Refresh conversation (Ctrl+R)"
                }
            }
        }

        Rectangle {
            visible: root.controller.errorMessage.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 38 : 0
            color: "#3a2025"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 8

                Text {
                    Layout.fillWidth: true
                    text: root.controller.errorMessage
                    color: "#efb1b2"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
                ToolButton {
                    text: "×"
                    onClicked: root.controller.clearError()
                }
            }
        }

        ListView {
            id: transcript
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: root.conversationModel
            clip: true
            spacing: 8
            leftMargin: 16
            rightMargin: 16
            topMargin: 14
            bottomMargin: 18
            reuseItems: true
            boundsBehavior: Flickable.StopAtBounds

            header: Item {
                width: transcript.width
                height: root.conversationModel.hasMore ? 42 : 8

                Button {
                    visible: root.conversationModel.hasMore
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.conversationModel.loading ? "Loading…" : "Load earlier messages"
                    enabled: !root.conversationModel.loading
                    onClicked: root.controller.loadOlderSession(root.session)
                }
            }

            delegate: MessageDelegate {
                showTools: root.controller.toolsVisible
            }

            footer: Item {
                width: transcript.width
                height: root.conversationModel.loading ? 34 : 6

                BusyIndicator {
                    anchors.centerIn: parent
                    running: root.conversationModel.loading
                    visible: running
                    implicitWidth: 22
                    implicitHeight: 22
                }
            }

            ScrollBar.vertical: ScrollBar {}

            Connections {
                target: root.conversationModel
                function onCountChanged() {
                    if (transcript.count > 0)
                        Qt.callLater(() => transcript.positionViewAtEnd());
                }
            }

            Label {
                anchors.centerIn: parent
                visible: transcript.count === 0 && !root.conversationModel.loading
                text: root.session.length > 0 ? "No messages yet. Start the conversation below." : "Choose an agent from the sidebar."
                color: "#6f6976"
                font.pixelSize: 13
            }
        }

        Composer {
            Layout.fillWidth: true
            controller: root.controller
            session: root.session
            onOpenConnection: root.openConnection()
        }
    }
}
