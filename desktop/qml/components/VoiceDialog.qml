pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    required property string agentName
    signal closeRequested
    color: "#e61a1b26"

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Rectangle {
        width: Math.min(560, parent.width - 48)
        height: Math.min(680, parent.height - 48)
        anchors.centerIn: parent
        radius: 20
        color: "#20212e"
        border.color: "#41445a"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 14

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: root.agentName + "’s voice"
                    color: "#c0caf5"
                    font.pixelSize: 21
                    font.weight: Font.DemiBold
                }
                ToolButton {
                    text: "×"
                    onClicked: root.closeRequested()
                }
            }

            Text {
                visible: text.length > 0
                Layout.fillWidth: true
                text: root.controller.voiceBio
                color: "#8d93b0"
                wrapMode: Text.Wrap
                font.pixelSize: 12
            }

            BusyIndicator {
                Layout.alignment: Qt.AlignHCenter
                visible: root.controller.voicesLoading
                running: visible
            }

            ListView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: root.controller.voices
                spacing: 7
                clip: true
                reuseItems: true

                delegate: Rectangle {
                    id: voiceRow
                    required property string voiceId
                    required property string label
                    required property string takenBy
                    required property bool current

                    width: ListView.view.width
                    implicitHeight: 54
                    radius: 11
                    color: current ? "#30263a" : "#201d25"
                    border.color: current ? "#704f82" : "#312c38"

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 14
                        anchors.rightMargin: 9
                        spacing: 8

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                text: voiceRow.label
                                color: "#e8e1e9"
                                font.pixelSize: 13
                                font.weight: Font.Medium
                            }
                            Text {
                                text: voiceRow.current ? "Current" : voiceRow.takenBy.length > 0 ? "Used by " + voiceRow.takenBy : "Available"
                                color: voiceRow.takenBy.length > 0 ? "#a77c7d" : "#77717f"
                                font.pixelSize: 10
                            }
                        }

                        Button {
                            text: "Preview"
                            onClicked: root.controller.previewVoice(root.session, root.agentName, voiceRow.voiceId)
                        }
                        Button {
                            text: "Use"
                            visible: !voiceRow.current && voiceRow.takenBy.length === 0
                            onClicked: root.controller.chooseVoice(root.session, voiceRow.voiceId)
                        }
                    }
                }

                ScrollBar.vertical: ScrollBar {}
            }
        }
    }
}
