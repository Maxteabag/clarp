pragma ComponentBehavior: Bound

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
    required property string agentId
    required property int backgroundJobCount
    required property int subAgentCount
    required property int runningChildren
    required property string agentRole
    required property string helperState
    // Nesting depth and collapsed finished helpers, from AgentFilterModel.
    required property int treeDepth
    required property var doneHelpers
    readonly property bool keyboardCurrent: ListView.isCurrentItem && ListView.view !== null && ListView.view.activeFocus
    property bool collapsed: false
    property bool archived: false
    signal chatSelected
    signal processesRequested(string session, Item anchor)
    signal doneHelpersToggled(string parentAgentId)
    readonly property int indent: collapsed ? 0 : treeDepth * 22
    readonly property bool finishedHelper: agentRole === "helper"
        && ["done", "reported", "abandoned"].includes(helperState)

    readonly property bool current: !archived && controller.selectedSession === session
    readonly property string activityLine: statusText.length > 0 ? statusText : busy ? "Working…" : ""

    width: ListView.view ? ListView.view.width : 280
    leftPadding: collapsed ? 0 : 14 + indent
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
        border.color: Theme.accent
        color: row.current ? Theme.control : row.hovered ? Theme.dangerSurface : "transparent"

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: row.collapsed ? 0 : 74 + row.indent
            height: 1
            color: Theme.raised
            visible: !row.current
        }

        // A helper hangs off its parent with a thin elbow, like a tree.
        Rectangle {
            objectName: "helperConnector"
            visible: row.treeDepth > 0 && !row.collapsed
            x: 14 + row.indent - 13
            y: 0
            width: 1
            height: row.topPadding + mainRow.height / 2
            color: Theme.border
        }
        Rectangle {
            visible: row.treeDepth > 0 && !row.collapsed
            x: 14 + row.indent - 13
            y: row.topPadding + mainRow.height / 2
            width: 9
            height: 1
            color: Theme.border
        }
    }

    contentItem: ColumnLayout {
        spacing: 0

    RowLayout {
        id: mainRow
        Layout.fillWidth: true
        spacing: 12

        AvatarActivity {
            id: activityAvatar
            working: row.busy
            Layout.alignment: Qt.AlignTop | Qt.AlignHCenter
            controller: row.controller
            name: row.name
            session: row.session
            cornerRadius: 24
            avatarSize: row.collapsed ? 38 : (row.treeDepth > 0 ? 36 : 48)
            showPortrait: true
            opacity: row.archived || row.finishedHelper ? 0.65 : 1

            ProcessIndicator {
                objectName: "collapsedProcessIndicator"
                visible: row.collapsed && total > 0
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.rightMargin: -3
                anchors.bottomMargin: -3
                glyphSize: 12
                jobCount: row.backgroundJobCount
                subAgentCount: row.subAgentCount
                runningChildren: row.runningChildren
                reducedMotion: activityAvatar.reducedMotion
                onClicked: row.processesRequested(row.session, this)
            }

            Rectangle {
                visible: row.collapsed && row.unread
                anchors.right: parent.right
                anchors.top: parent.top
                width: 12
                height: 12
                radius: 6
                color: Theme.accent
                border.color: Theme.raised
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
                    color: Theme.text
                    font.pixelSize: 14
                    font.weight: row.unread ? Font.DemiBold : Font.Medium
                    elide: Text.ElideRight
                }

                ProcessIndicator {
                    id: processIndicator
                    objectName: "sidebarProcessIndicator"
                    Layout.alignment: Qt.AlignVCenter
                    jobCount: row.backgroundJobCount
                    subAgentCount: row.subAgentCount
                    runningChildren: row.runningChildren
                    reducedMotion: activityAvatar.reducedMotion
                    onClicked: row.processesRequested(row.session, processIndicator)
                }

                TuiText {
                    text: row.controller.chatStamp(row.lastActivity)
                    color: row.unread ? Theme.accent : Theme.faint
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
                    color: row.unread ? Theme.secondary : Theme.faint
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }

                TuiText {
                    visible: row.muted
                    text: "⃠"
                    color: Theme.faint
                    font.pixelSize: 11
                }

                TuiText {
                    visible: row.queueCount > 0
                    text: row.queueCount + " queued"
                    color: Theme.warning
                    font.pixelSize: 10
                }

                Rectangle {
                    visible: row.unread
                    implicitWidth: Math.max(18, unreadLabel.implicitWidth + 10)
                    implicitHeight: 18
                    radius: 9
                    color: Theme.accent

                    TuiText {
                        id: unreadLabel

                        anchors.centerIn: parent
                        text: row.unreadCount > 0 ? row.unreadCount : ""
                        color: Theme.window
                        font.pixelSize: 10
                        font.weight: Font.Bold
                    }
                }
            }

            TuiText {
                visible: !row.archived && row.activityLine.length > 0
                Layout.fillWidth: true
                text: row.activityLine
                ActivitySweep { anchors.fill: parent; working: activityAvatar.authoritativeWorking; reducedMotion: activityAvatar.reducedMotion; phase: activityAvatar.phase }
                color: row.busy ? Theme.warning : Theme.muted
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

    // "N helpers done" for the finished helpers collapsed below this row.
    Repeater {
        model: row.collapsed ? [] : row.doneHelpers

        AbstractButton {
            id: doneLine
            required property var modelData
            objectName: "doneHelpersLine"
            Layout.fillWidth: true
            Layout.leftMargin: (Number(doneLine.modelData.depth || 0) + 1) * 22 - row.indent + 8
            Layout.topMargin: 6
            implicitHeight: 22
            hoverEnabled: true
            readonly property int count: Number(doneLine.modelData.count || 0)
            text: (doneLine.modelData.expanded ? "▾ " : "▸ ") + count
                + (count === 1 ? " helper done" : " helpers done")
            Accessible.name: text
            onClicked: row.doneHelpersToggled(String(doneLine.modelData.parentAgentId || ""))
            background: Rectangle { color: doneLine.hovered ? Theme.control : "transparent" }
            contentItem: TuiText {
                text: doneLine.text
                color: doneLine.hovered ? Theme.secondary : Theme.faint
                font.pixelSize: 11
                verticalAlignment: Text.AlignVCenter
            }
        }
    }
    }
}
