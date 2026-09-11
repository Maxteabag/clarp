pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Sidebar row for an agent-to-agent conversation. The room is a read-only
// Host projection keyed by both agents' stable ids, so renames keep the row.
ItemDelegate {
    id: row

    required property var controller
    required property var room
    readonly property string conversationId: String(room.conversation_id || "")
    readonly property var participants: room.participants || []
    readonly property var latest: room.latest_message || ({})
    readonly property bool unread: Boolean(room.unread)
    readonly property bool current: controller.selectedSession === row.conversationId
    readonly property string preview: {
        const text = String(row.latest.text || "");
        const sender = String(row.latest.sender_name || "");
        return text.length > 0 ? (sender.length > 0 ? sender + ": " : "") + text : "No messages yet";
    }
    signal chatSelected

    objectName: "pairRow"
    width: parent ? parent.width : 280
    leftPadding: 14
    rightPadding: 14
    topPadding: 8
    bottomPadding: 8
    hoverEnabled: true
    Accessible.name: String(room.title || "Agent conversation")
    onClicked: {
        row.controller.selectSession(row.conversationId);
        row.chatSelected();
    }

    background: Rectangle {
        color: row.current ? "#292b3a" : row.hovered ? "#211e27" : "transparent"
    }

    contentItem: RowLayout {
        spacing: 12

        Item {
            Layout.preferredWidth: 52
            Layout.preferredHeight: 44
            Layout.alignment: Qt.AlignTop
            Repeater {
                model: row.participants.slice(0, 2)
                AgentAvatar {
                    required property var modelData
                    required property int index
                    x: index * 18
                    y: index * 10
                    z: 2 - index
                    controller: row.controller
                    session: String(modelData.session || "")
                    name: String(modelData.name || "Agent")
                    avatarSize: 32
                    cornerRadius: 16
                    fallbackColor: index === 0 ? "#555970" : "#3b3e50"
                }
            }
            Rectangle {
                objectName: "pairUnreadDot"
                visible: row.unread
                anchors.right: parent.right
                anchors.top: parent.top
                width: 12
                height: 12
                radius: 0
                color: "#bb9af7"
                border.color: "#20212e"
                border.width: 2
                z: 5
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 3
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                TuiText {
                    objectName: "pairTitle"
                    Layout.fillWidth: true
                    text: String(row.room.title || "Agent conversation")
                    color: "#eee8e2"
                    font.pixelSize: 14
                    font.weight: row.unread ? Font.DemiBold : Font.Medium
                    elide: Text.ElideRight
                }
                TuiText {
                    text: row.controller.chatStamp(Number(row.room.latest_activity || 0))
                    color: row.unread ? "#c69ade" : "#77717f"
                    font.pixelSize: 10
                }
            }
            TuiText {
                objectName: "pairPreview"
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: row.preview
                color: row.unread ? "#c9c3cf" : "#8d8794"
                font.pixelSize: 12
                elide: Text.ElideRight
                maximumLineCount: 1
            }
        }
    }
}
