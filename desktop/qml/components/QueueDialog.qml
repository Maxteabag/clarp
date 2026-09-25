pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string session: ""
    signal closeRequested
    color: Qt.alpha(Theme.sunken, 0.69)

    MouseArea {
        anchors.fill: parent
        onClicked: root.closeRequested()
    }

    Rectangle {
        width: Math.min(660, parent.width - 40)
        height: Math.min(620, parent.height - 60)
        anchors.centerIn: parent
        radius: 0
        color: Theme.raised
        border.color: Theme.faint

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
                    TuiText {
                        text: "QUEUED MESSAGES"
                        color: Theme.text
                        font.family: "JetBrains Mono"
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.2
                    }
                    TuiText {
                        text: root.controller.agentName(root.session)
                            + (root.controller.turnQueuePaused ? "  ·  paused" : "")
                        color: root.controller.turnQueuePaused ? Theme.warning : Theme.muted
                        font.pixelSize: 9
                    }
                }
                TuiBusyIndicator {
                    visible: root.controller.turnQueueLoading
                    running: visible
                    implicitWidth: 18
                    implicitHeight: 18
                }
                TuiToolButton {
                    text: "↻"
                    onClicked: root.controller.loadTurnQueue(root.session)
                }
                TuiToolButton { text: "×"; onClicked: root.closeRequested() }
            }

            TuiText {
                visible: root.controller.turnQueueError.length > 0
                Layout.fillWidth: true
                text: root.controller.turnQueueError
                color: Theme.danger
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
                    radius: 0
                    color: Theme.control
                    border.color: Theme.border

                    ColumnLayout {
                        id: queueColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 9
                        spacing: 7

                        TuiTextArea {
                            id: queuedText
                            Layout.fillWidth: true
                            text: String(queueRow.modelData.text || "")
                            wrapMode: TextArea.Wrap
                            selectByMouse: true
                            font.pixelSize: 11
                            color: Theme.text
                            background: Rectangle {
                                radius: 0
                                color: Theme.raised
                                border.color: queuedText.activeFocus ? Theme.muted : Theme.border
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            TuiText {
                                Layout.fillWidth: true
                                text: String(queueRow.modelData.enqueued_at || "")
                                color: Theme.faint
                                font.family: "JetBrains Mono"
                                font.pixelSize: 8
                                elide: Text.ElideRight
                            }
                            TuiButton {
                                text: "Save"
                                enabled: !root.controller.turnQueueLoading
                                    && queuedText.text.trim().length > 0
                                    && queuedText.text !== String(queueRow.modelData.text || "")
                                onClicked: root.controller.updateQueuedTurn(
                                    String(queueRow.modelData.id || ""), queuedText.text)
                            }
                            TuiButton {
                                text: "Send now"
                                enabled: !root.controller.turnQueueLoading
                                onClicked: root.controller.sendQueuedTurn(
                                    String(queueRow.modelData.id || ""))
                            }
                            TuiToolButton {
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

                TuiLabel {
                    anchors.centerIn: parent
                    visible: queueList.count === 0 && !root.controller.turnQueueLoading
                    text: "No queued messages"
                    color: Theme.faint
                    font.pixelSize: 10
                }
            }
        }
    }
}
