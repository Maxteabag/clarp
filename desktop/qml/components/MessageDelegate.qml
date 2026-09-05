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
    readonly property var narrator: controller.toolNarrator || null
    readonly property bool narrationEnabled: narrator !== null && narrator.enabled
    readonly property bool localFilesAllowed: Boolean(controller.sharedFilesystem)
    readonly property string workingDirectory: {
        controller.agentRevision;
        return narrationEnabled && localFilesAllowed ? controller.agentWorkingDirectory(session) : "";
    }
    readonly property int presentedActivityCount: Math.max(
        root.activityCount, root.displayCells.length + root.tools.length)
    readonly property bool showActivityCards: root.showTools
        || root.activityExpanded || root.presentedActivityCount === 1 || root.narrationEnabled
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

    width: ListView.view ? ListView.view.width - ListView.view.leftMargin
        - ListView.view.rightMargin : 600
    visible: activity || body.length > 0 || displayCells.length > 0
        || presentedActivityCount > 0 || (showTools && tools.length > 0)
    implicitHeight: visible ? content.implicitHeight + (activity || body.length === 0 ? 2 : 6) : 0

    TextMetrics {
        id: bubbleMetrics
        font.pixelSize: 15
        text: root.body
    }

    ActivityExplanation {
        session: root.session
        id: liveExplanation
        narrator: root.narrator
        active: root.visible && root.activity && root.toolName.length > 0
        activity: ({name: root.toolName, summary: root.body})
        workingDirectory: root.workingDirectory
        localFilesAllowed: root.localFilesAllowed
    }

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 2

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
            visible: root.activity || root.body.length > 0
            Layout.fillWidth: true
            implicitHeight: root.activity ? activityCard.implicitHeight : messageBubble.visible ? messageBubble.implicitHeight : 0

            Rectangle {
                id: activityCard
                visible: root.activity
                x: 0
                width: parent.width
                implicitHeight: Math.max(24, activityRow.implicitHeight + 4)
                radius: 2
                color: "transparent"
                border.width: 0

                HoverHandler { id: liveActivityHover }
                ToolTip.visible: liveActivityHover.hovered && liveExplanation.text.length > 0
                ToolTip.text: root.toolName + " · " + root.body
                ToolTip.delay: 400

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
                        visible: !liveExplanation.enabled
                        Layout.maximumWidth: activityRow.width * 0.3
                        elide: Text.ElideRight
                        text: root.toolName || root.messageKind || "Working"
                        color: "#868a9f"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: liveExplanation.enabled ? liveExplanation.displayText : root.body
                        textFormat: Text.PlainText
                        color: liveExplanation.enabled ? "#82aaff" : "#969bb5"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        wrapMode: liveExplanation.enabled ? Text.Wrap : Text.NoWrap
                        elide: liveExplanation.enabled ? Text.ElideNone : Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: messageBubble
                objectName: "userMessageBackground"
                visible: !root.activity && root.body.length > 0
                width: Math.min(parent.width * (root.userAuthored ? 0.78 : 0.95), 840,
                    Math.max(140, bubbleMetrics.advanceWidth + 28))
                x: root.userAuthored ? parent.width - width : 0
                implicitHeight: messageBlocks.implicitHeight + 24
                radius: 14
                color: root.userAuthored ? "#493651" : "#1b191f"
                border.width: root.deliveryFailed ? 1 : 0
                border.color: "#8d5763"
                opacity: root.pending ? 0.68 : 1

                Column {
                    id: messageBlocks
                    x: 12
                    y: 12
                    width: parent.width - 24
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
                            color: "#e7e1dc"
                            selectedTextColor: "#fff8ff"
                            selectionColor: "#6f527b"
                            font.pixelSize: 15
                            onLinkActivated: link => Qt.openUrlExternally(link)
                        }
                    }
                }
            }
        }

        Rectangle {
            visible: !root.activity && root.presentedActivityCount > 1 && !root.showTools
                && !root.narrationEnabled
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
                font.pixelSize: 12
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
            spacing: 1

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
                    session: root.session
                    required property var modelData
                    Layout.fillWidth: true
                    cell: modelData
                    narrator: root.narrator
                    workingDirectory: root.workingDirectory
                    localFilesAllowed: root.localFilesAllowed
                }
            }

            Repeater {
                model: root.tools

                ToolCard {
                    session: root.session
                    required property var modelData
                    visible: root.displayCells.length === 0
                        || ["Edit", "MultiEdit", "Write"].includes(
                            String(modelData.name || ""))
                    Layout.preferredHeight: visible ? implicitHeight : 0
                    Layout.fillWidth: true
                    tool: modelData
                    narrator: root.narrator
                    workingDirectory: root.workingDirectory
                    localFilesAllowed: root.localFilesAllowed
                }
            }
        }

        Text {
            visible: root.showTimestamp && root.timestamp.length > 0
                && !root.activity
            Layout.alignment: root.userAuthored ? Qt.AlignRight : Qt.AlignLeft
            Layout.leftMargin: 12
            Layout.rightMargin: 12
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
