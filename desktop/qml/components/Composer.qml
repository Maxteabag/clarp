import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    readonly property int agentRevision: controller.agentRevision
    signal openConnection

    implicitHeight: 94
    color: "#151319"
    border.color: "#28242e"
    border.width: 1

    RowLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 10

        ToolButton {
            text: root.controller.muted ? "M" : "♪"
            implicitWidth: 42
            implicitHeight: 42
            onClicked: root.controller.muted = !root.controller.muted
            ToolTip.visible: hovered
            ToolTip.text: root.controller.muted ? "Voice replies muted" : "Voice replies enabled"
        }

        ToolButton {
            id: recordButton
            text: root.controller.audio.transcribing ? "…" : root.controller.audio.recording ? "■" : "●"
            enabled: !root.controller.audio.transcribing && root.session.length > 0
            implicitWidth: 42
            implicitHeight: 42
            onClicked: root.controller.audio.toggleRecording()
            ToolTip.visible: hovered
            ToolTip.text: root.controller.audio.recording ? "Stop and transcribe" : "Record a voice message"

            contentItem: Text {
                text: recordButton.text
                color: root.controller.audio.recording ? "#e57e7f" : "#b8aebc"
                font.pixelSize: 14
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        ToolButton {
            visible: root.controller.audio.playing || root.controller.audio.paused
            text: "■"
            implicitWidth: 42
            implicitHeight: 42
            onClicked: root.controller.audio.silence()
            ToolTip.visible: hovered
            ToolTip.text: "Stop current voice playback"
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 13
            color: "#1d1a22"
            border.color: editor.activeFocus ? "#765787" : "#302b37"
            border.width: 1

            TextArea {
                id: editor
                anchors.fill: parent
                anchors.margins: 3
                placeholderText: root.session.length > 0 ? "Message " + root.controller.agentName(root.session) + "…" : "Choose an agent"
                enabled: root.session.length > 0
                wrapMode: TextArea.Wrap
                color: "#eee8e2"
                font.pixelSize: 14
                background: null
                leftPadding: 10
                rightPadding: 10
                topPadding: 9
                bottomPadding: 9

                Keys.onPressed: event => {
                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                        if (editor.text.trim().length > 0) {
                            root.controller.sendMessageTo(root.session, editor.text, false);
                            editor.clear();
                        }
                        event.accepted = true;
                    }
                }
            }
        }

        Button {
            text: "Stop"
            visible: {
                root.agentRevision;
                const state = root.controller.agentState(root.session);
                return state === "thinking" || state === "tool" || state === "compacting";
            }
            onClicked: root.controller.stopSession(root.session)
        }

        Button {
            id: sendButton
            text: "Send"
            enabled: editor.text.trim().length > 0 && root.session.length > 0
            implicitWidth: 74
            implicitHeight: 42
            onClicked: {
                root.controller.sendMessageTo(root.session, editor.text, false);
                editor.clear();
                editor.forceActiveFocus();
            }

            background: Rectangle {
                radius: 12
                color: sendButton.enabled ? "#bd8ad7" : "#302b37"
            }
            contentItem: Text {
                text: sendButton.text
                color: sendButton.enabled ? "#171119" : "#716a78"
                font.weight: Font.DemiBold
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        ToolButton {
            text: "⋮"
            implicitWidth: 38
            onClicked: root.openConnection()
            ToolTip.visible: hovered
            ToolTip.text: "Connection settings"
        }
    }
}
