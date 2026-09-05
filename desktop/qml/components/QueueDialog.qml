pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string session: ""
    signal closeRequested
    color: "#b008090f"

    MouseArea {
        anchors.fill: parent
        onClicked: root.closeRequested()
    }

    Rectangle {
        width: Math.min(660, parent.width - 40)
        height: Math.min(620, parent.height - 60)
        anchors.centerIn: parent
        radius: 7
        color: "#181a24"
        border.color: "#42465f"

        MouseArea {
            anchors.fill: parent
            onClicked: mouse => mouse.accepted = true
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 9

            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Text {
                        text: "QUEUED MESSAGES"
                        color: "#c9cde3"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.2
                    }
                    Text {
                        text: root.controller.agentName(root.session)
                            + (root.controller.turnQueuePaused ? "  ·  paused" : "")
                        color: root.controller.turnQueuePaused ? "#b18b70" : "#686d84"
                        font.pixelSize: 9
                    }
                }
                BusyIndicator {
                    visible: root.controller.turnQueueLoading
                    running: visible
                    implicitWidth: 18
                    implicitHeight: 18
                }
                ToolButton {
                    text: "↻"
                    onClicked: root.controller.loadTurnQueue(root.session)
                }
                ToolButton { text: "×"; onClicked: root.closeRequested() }
            }

            Text {
                visible: root.controller.turnQueueError.length > 0
                Layout.fillWidth: true
                text: root.controller.turnQueueError
                color: "#c98a98"
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }

            ListView {
                id: queueList
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: root.controller.turnQueueSession === root.session
                    ? root.controller.turnQueueItems : []
                spacing: 7
                clip: true
                reuseItems: true

                delegate: Rectangle {
                    id: queueRow
                    required property var modelData
                    width: ListView.view.width
                    implicitHeight: queueColumn.implicitHeight + 18
                    radius: 5
                    color: "#202330"
                    border.color: "#34384d"

                    ColumnLayout {
                        id: queueColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 9
                        spacing: 7

                        TextArea {
                            id: queuedText
                            Layout.fillWidth: true
                            text: String(queueRow.modelData.text || "")
                            wrapMode: TextArea.Wrap
                            selectByMouse: true
                            font.pixelSize: 11
                            color: "#c2c5d7"
                            background: Rectangle {
                                radius: 3
                                color: "#1b1d28"
                                border.color: queuedText.activeFocus ? "#737aa3" : "#303448"
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                text: String(queueRow.modelData.enqueued_at || "")
                                color: "#62677e"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 8
                                elide: Text.ElideRight
                            }
                            Button {
                                text: "Save"
                                enabled: !root.controller.turnQueueLoading
                                    && queuedText.text.trim().length > 0
                                    && queuedText.text !== String(queueRow.modelData.text || "")
                                onClicked: root.controller.updateQueuedTurn(
                                    String(queueRow.modelData.id || ""), queuedText.text)
                            }
                            Button {
                                text: "Send now"
                                enabled: !root.controller.turnQueueLoading
                                onClicked: root.controller.sendQueuedTurn(
                                    String(queueRow.modelData.id || ""))
                            }
                            ToolButton {
                                text: "⌫"
                                enabled: !root.controller.turnQueueLoading
                                onClicked: root.controller.deleteQueuedTurn(
                                    String(queueRow.modelData.id || ""))
                                ToolTip.visible: hovered
                                ToolTip.text: "Remove queued message"
                            }
                        }
                    }
                }

                Label {
                    anchors.centerIn: parent
                    visible: queueList.count === 0 && !root.controller.turnQueueLoading
                    text: "No queued messages"
                    color: "#62677e"
                    font.pixelSize: 10
                }
            }
        }
    }
}
