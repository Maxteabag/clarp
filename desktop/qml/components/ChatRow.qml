import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ItemDelegate {
    id: row

    required property var controller
    required property string session
    required property string name
    required property string backend
    required property string workingDirectory
    required property string avatarUrl
    required property string lastMessage
    required property string lastCompletedMessage
    readonly property string messagePreview: controller.showWhenReady ? lastCompletedMessage : lastMessage
    required property string agentState
    required property string statusText
    required property real lastActivity
    required property bool busy
    required property bool unread
    required property bool muted
    property int unreadCount: 0
    required property int queueCount
    readonly property bool keyboardCurrent: ListView.isCurrentItem && ListView.view !== null && ListView.view.activeFocus
    property bool collapsed: false
    property bool archived: false
    signal chatSelected

    readonly property bool current: !archived && controller.selectedSession === session
    readonly property string activityLine: statusText.length > 0 ? statusText : busy ? "Working…" : ""

    width: ListView.view ? ListView.view.width : 280
    leftPadding: collapsed ? 0 : 14
    rightPadding: collapsed ? 0 : 14
    topPadding: 9
    bottomPadding: 9
    hoverEnabled: true
    onClicked: {
        if (row.archived)
            return;
        row.controller.selectSession(row.session);
        row.chatSelected();
        row.controller.requestComposerFocus(row.controller.panes.activePaneId);
    }

    background: Rectangle {
        border.width: row.keyboardCurrent ? 1 : 0
        border.color: "#bb9af7"
        color: row.current ? "#292b3a" : row.hovered ? "#211e27" : "transparent"

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: row.collapsed ? 0 : 74
            height: 1
            color: "#201d26"
            visible: !row.current
        }
    }

    contentItem: RowLayout {
        spacing: 12

        AgentAvatar {
            Layout.alignment: Qt.AlignTop | Qt.AlignHCenter
            controller: row.controller
            name: row.name
            session: row.session
            cornerRadius: 24
            avatarSize: row.collapsed ? 38 : 48
            opacity: row.archived ? 0.65 : 1

            Rectangle {
                visible: row.collapsed && row.unread
                anchors.right: parent.right
                anchors.top: parent.top
                width: 12
                height: 12
                radius: 0
                color: "#bb9af7"
                border.color: "#20212e"
                border.width: 2
            }
        }

        ColumnLayout {
            visible: !row.collapsed
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 3

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                TuiText {
                    Layout.fillWidth: true
                    text: row.name
                    color: "#eee8e2"
                    font.pixelSize: 14
                    font.weight: row.unread ? Font.DemiBold : Font.Medium
                    elide: Text.ElideRight
                }

                TuiText {
                    text: row.controller.chatStamp(row.lastActivity)
                    color: row.unread ? "#c69ade" : "#77717f"
                    font.pixelSize: 10
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                TuiText {
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    objectName: "sidebarMessagePreview"
                    text: row.messagePreview.length > 0 ? row.messagePreview : (row.workingDirectory.length > 0 ? row.workingDirectory : row.backend)
                    color: row.unread ? "#b3aab9" : "#77717f"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }

                TuiText {
                    visible: row.muted
                    text: "⃠"
                    color: "#77717f"
                    font.pixelSize: 11
                }

                TuiText {
                    visible: row.queueCount > 0
                    text: row.queueCount + " queued"
                    color: "#dba163"
                    font.pixelSize: 10
                }

                Rectangle {
                    visible: row.unread
                    implicitWidth: Math.max(18, unreadLabel.implicitWidth + 10)
                    implicitHeight: 18
                    radius: 0
                    color: "#bb9af7"

                    TuiText {
                        id: unreadLabel

                        anchors.centerIn: parent
                        text: row.unreadCount > 0 ? row.unreadCount : ""
                        color: "#1a1b26"
                        font.pixelSize: 10
                        font.weight: Font.Bold
                    }
                }
            }

            TuiText {
                visible: !row.archived && row.activityLine.length > 0
                Layout.fillWidth: true
                text: row.activityLine
                color: row.busy ? "#dba163" : "#8d93b0"
                font.pixelSize: 10
                elide: Text.ElideRight
            }
        }

        TuiButton {
            visible: row.archived && !row.collapsed
            text: "Restore"
            onClicked: row.controller.setAgentArchived(row.session, false)
        }
    }
}
