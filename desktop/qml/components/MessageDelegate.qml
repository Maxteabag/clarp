pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    required property var controller
    required property string session
    required property string messageId
    required property string authorRole
    required property string body
    required property string timestamp
    required property string messageKind
    required property string toolName
    required property string origin
    required property string senderName
    required property bool pending
    required property bool deliveryFailed
    required property bool activity
    required property string activityStatus
    required property bool automated
    required property string category
    required property var tools
    required property var displayCells
    required property int activityCount
    required property bool toolDetailsAvailable
    required property bool showTools
    required property bool showTimestamp
    property bool activityExpanded: false
    readonly property int presentedActivityCount: Math.max(
        root.activityCount, root.displayCells.length + root.tools.length)
    readonly property bool showActivityCards: root.showTools
        || root.activityExpanded || root.presentedActivityCount === 1
    readonly property bool userAuthored: root.authorRole === "user"
        && root.origin !== "agent" && root.origin !== "automation"
    readonly property int mediaRevision: controller.mediaRevision
    readonly property string renderedBody: {
        mediaRevision;
        return controller.resolveMediaMarkdown(body);
    }
    readonly property var renderedBlocks: root.messageKind === "live"
        ? [root.renderedBody]
        : root.controller.markdownDisplayBlocks(root.renderedBody)

    width: ListView.view ? ListView.view.width : 600
    visible: activity || body.length > 0 || displayCells.length > 0
        || presentedActivityCount > 0 || (showTools && tools.length > 0)
    implicitHeight: visible ? content.implicitHeight + 8 : 0

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 4

        Text {
            visible: !root.activity && (root.origin === "agent"
                || root.origin === "automation" || root.automated)
            Layout.leftMargin: 2
            text: root.origin === "agent"
                ? ((root.senderName || "Agent") + " · TEAM")
                : ((root.category || "AUTOMATION").toUpperCase())
            color: root.origin === "agent" ? "#8f96bc" : "#8a806f"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
            font.weight: Font.DemiBold
            font.letterSpacing: 0.7
        }

        Item {
            Layout.fillWidth: true
            implicitHeight: root.activity ? 24 : messageBubble.visible ? messageBubble.implicitHeight : 0

            Rectangle {
                id: activityCard
                visible: root.activity
                x: 0
                width: parent.width
                implicitHeight: 24
                radius: 2
                color: "transparent"
                border.width: 0

                RowLayout {
                    id: activityRow
                    anchors.fill: parent
                    anchors.leftMargin: 3
                    anchors.rightMargin: 3
                    spacing: 7

                    Rectangle {
                        id: activityDot
                        implicitWidth: 6
                        implicitHeight: 6
                        radius: 3
                        color: root.activityStatus === "error" ? "#bd7484" : "#89a879"
                        opacity: 1

                        SequentialAnimation on opacity {
                            running: root.activity && root.activityStatus === "running"
                            loops: Animation.Infinite
                            NumberAnimation { to: 0.32; duration: 520 }
                            NumberAnimation { to: 1; duration: 520 }
                        }
                    }
                    Text {
                        text: root.toolName || root.messageKind || "Working"
                        color: "#868a9f"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.body
                        color: "#696c82"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: messageBubble
                visible: !root.activity && root.body.length > 0
                width: Math.min(parent.width, 840)
                x: 0
                implicitHeight: messageBlocks.implicitHeight + (root.userAuthored ? 18 : 12)
                radius: 3
                color: root.userAuthored ? "#242737" : "transparent"
                border.width: root.deliveryFailed ? 1 : 0
                border.color: "#8d5763"
                opacity: root.pending ? 0.68 : 1

                Rectangle {
                    visible: root.userAuthored
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: 2
                    color: root.deliveryFailed ? "#a45e6d" : "#555a73"
                }

                Column {
                    id: messageBlocks
                    x: root.userAuthored ? 10 : 2
                    y: root.userAuthored ? 9 : 6
                    width: parent.width - x - 8
                    spacing: 8

                    Repeater {
                        model: root.renderedBlocks

                        TextEdit {
                            required property string modelData
                            width: messageBlocks.width
                            text: modelData
                            readOnly: true
                            selectByMouse: true
                            persistentSelection: true
                            // Qt's Markdown parser is not incremental-safe when a
                            // stream ends halfway through a fence/list/tag. Present
                            // growing text plainly; the finalized row upgrades to
                            // Markdown without changing model identity.
                            textFormat: root.messageKind === "live"
                                ? Text.PlainText : Text.MarkdownText
                            wrapMode: Text.Wrap
                            color: "#b9bbcf"
                            font.pixelSize: 13
                            onLinkActivated: link => Qt.openUrlExternally(link)
                        }
                    }
                }
            }
        }

        Rectangle {
            visible: !root.activity && root.presentedActivityCount > 1 && !root.showTools
            Layout.fillWidth: true
            implicitHeight: visible ? 24 : 0
            radius: 3
            color: activityTap.hovered ? "#202335" : "transparent"

            Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.activityExpanded
                    ? root.presentedActivityCount + " activities"
                    : "+" + root.presentedActivityCount + " more"
                color: "#72778f"
                font.family: "JetBrains Mono"
                font.pixelSize: 10
            }

            HoverHandler { id: activityTap }
            TapHandler {
                onTapped: {
                    if (!root.activityExpanded && root.toolDetailsAvailable)
                        root.controller.loadMessageToolDetails(root.session, root.messageId);
                    root.activityExpanded = !root.activityExpanded;
                }
            }
        }

        ColumnLayout {
            visible: !root.activity && root.showActivityCards
                && root.presentedActivityCount > 0
            Layout.fillWidth: true
            Layout.leftMargin: 2
            Layout.rightMargin: 2
            spacing: 3

            Button {
                visible: root.toolDetailsAvailable
                    && root.displayCells.length === 0 && root.tools.length === 0
                text: "Load activity details"
                implicitHeight: 26
                onClicked: root.controller.loadMessageToolDetails(
                    root.session, root.messageId)
            }

            Repeater {
                model: root.displayCells

                DisplayCellCard {
                    required property var modelData
                    Layout.fillWidth: true
                    cell: modelData
                }
            }

            Repeater {
                model: root.tools

                ToolCard {
                    required property var modelData
                    visible: root.displayCells.length === 0
                        || ["Edit", "MultiEdit", "Write"].includes(
                            String(modelData.name || ""))
                    Layout.preferredHeight: visible ? implicitHeight : 0
                    Layout.fillWidth: true
                    tool: modelData
                }
            }
        }

        Text {
            visible: root.showTimestamp && root.timestamp.length > 0
                && !root.activity
            Layout.leftMargin: root.userAuthored ? 10 : 2
            text: Qt.formatDateTime(new Date(root.timestamp), "MMM d  HH:mm")
            color: "#555a70"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
        }

        RowLayout {
            visible: root.pending || root.deliveryFailed
            Layout.alignment: root.userAuthored ? Qt.AlignRight : Qt.AlignLeft
            Layout.leftMargin: 4
            Layout.rightMargin: 4
            spacing: 6
            Text {
                text: root.deliveryFailed ? "Not delivered" : "Delivering…"
                color: root.deliveryFailed ? "#b56f7c" : "#5f6278"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
            }
            Button {
                visible: root.deliveryFailed
                text: "Retry"
                implicitHeight: 22
                onClicked: root.controller.retryFailedMessage(root.session, root.messageId)
            }
        }
    }
}
