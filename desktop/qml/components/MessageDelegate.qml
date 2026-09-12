pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property string linkOriginHost: ""
    Component.onCompleted: linkOriginHost = controller && controller.baseUrl ? String(controller.baseUrl) : ""

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
    property string senderAgentId: ""
    property string senderSession: ""
    property string replyToAgentId: ""
    property string replyToName: ""
    property string replyToSession: ""
    property string delivery: ""
    // Pair rooms show every row as an authored group message with its avatar.
    property bool groupView: false
    property int explanationRepeat: 1
    property string activitySummary: ""
    // Only an incoming prompt written by another agent is that agent's message.
    // The current agent's answer to it stays the current agent's own row.
    readonly property bool teamAuthored: root.origin === "agent" && root.authorRole === "user" && !root.groupView
    readonly property bool replyMarkerVisible: !root.activity && root.body.length > 0
        && root.replyToName.length > 0 && root.replyMarkerText.length > 0 && !root.teamAuthored
    readonly property string replyMarkerText: root.delivery === "private"
        ? "Not sent to " + root.replyToName
        : root.delivery === "pending" ? "Sending to " + root.replyToName
        : root.delivery === "failed" ? "Not delivered to " + root.replyToName
        : root.delivery === "sent" ? (root.groupView ? "" : "Reply to " + root.replyToName)
        : "Delivery unknown"
    readonly property bool rightAligned: !root.groupView && (root.userAuthored || root.teamAuthored)
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
    property string groupSummary: ""
    property bool groupedExpanded: false
    property bool forceActivityInline: false
    signal toggleActivityGroup()
    function loadInlineDetails() {
        if ((root.forceActivityInline || root.showTools) && root.toolDetailsAvailable
            && root.displayCells.length === 0 && root.tools.length === 0)
            root.controller.loadMessageToolDetails(root.session, root.messageId);
    }
    onForceActivityInlineChanged: Qt.callLater(root.loadInlineDetails)
    onToolDetailsAvailableChanged: Qt.callLater(root.loadInlineDetails)
    onMessageIdChanged: {
        root.activityExpanded = false;
        Qt.callLater(root.loadInlineDetails);
    }
    readonly property var narrator: controller.toolNarrator || null
    readonly property bool narrationEnabled: narrator !== null && narrator.enabled
    readonly property bool localFilesAllowed: Boolean(controller.sharedFilesystem)
    readonly property string workingDirectory: {
        controller.agentRevision;
        return narrationEnabled && localFilesAllowed ? controller.agentWorkingDirectory(session) : "";
    }
    readonly property int presentedActivityCount: Math.max(
        root.activityCount, root.displayCells.length + root.tools.length)
    readonly property bool showActivityCards: root.groupSummary.length > 0 ? root.groupedExpanded
        : root.showTools || root.forceActivityInline || root.activityExpanded
    readonly property bool userAuthored: !root.groupView && root.authorRole === "user"
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
        font.family: "JetBrains Mono"
        font.pixelSize: 15
        // Long messages already use the available width; do not shape the
        // entire growing stream a second time just to measure the bubble.
        text: root.body.length <= 160 ? root.body : ""
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

        TuiText {
            objectName: "messageProvenance"
            visible: !root.activity && !root.teamAuthored
                && (root.origin === "automation" || root.automated)
            Layout.leftMargin: 2
            text: (root.category || "AUTOMATION").toUpperCase()
            color: root.origin === "agent" ? "#8f96bc" : "#8a806f"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
            font.weight: Font.DemiBold
            font.letterSpacing: 0.7
        }

        RowLayout {
            objectName: "groupAuthorLine"
            visible: (root.groupView || root.teamAuthored) && !root.activity && root.body.length > 0
            Layout.fillWidth: true
            Layout.leftMargin: 2
            spacing: 6
            TuiText {
                objectName: "groupAuthorName"
                text: root.senderName || "Agent"
                color: "#c7adf1"
                font.pixelSize: 12
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.maximumWidth: parent.width * 0.5
            }
            TuiText {
                objectName: "groupReplyMarker"
                visible: root.replyToName.length > 0 && root.replyMarkerText.length > 0
                text: root.replyMarkerText
                color: "#8f96bc"
                font.pixelSize: 11
                elide: Text.ElideRight
                Layout.fillWidth: true
                HoverHandler { id: privateHover }
                ToolTip.visible: privateHover.hovered && root.delivery === "private"
                ToolTip.delay: 400
                ToolTip.text: "Answered in " + (root.senderName || "this agent") + "'s own chat. It was not sent to "
                    + root.replyToName + " unless " + (root.senderName || "the agent") + " messaged them."
            }
        }

        TuiText {
            objectName: "replyMarker"
            visible: root.replyMarkerVisible && !root.groupView
            Layout.leftMargin: 2
            text: root.replyMarkerText
            color: "#8f96bc"
            font.pixelSize: 11
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        Item {
            visible: root.activity || root.body.length > 0
            Layout.fillWidth: true
            implicitHeight: root.activity ? activityCard.implicitHeight : messageBubble.visible
                ? messageBubble.implicitHeight : 0

            Rectangle {
                id: activityCard
                visible: root.activity
                x: 0
                width: parent.width
                implicitHeight: Math.max(24, activityRow.implicitHeight + 4)
                radius: 0
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

                    TuiText {
                        visible: !liveExplanation.narrationShown
                        Layout.maximumWidth: activityRow.width * 0.3
                        elide: Text.ElideRight
                        text: root.toolName || root.messageKind || "Working"
                        color: "#868a9f"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                    TuiText {
                        Layout.fillWidth: true
                        text: (root.activityStatus === "error" ? "Error · " : "")
                            + (liveExplanation.narrationShown ? liveExplanation.displayText + (root.explanationRepeat > 1 ? " (x" + root.explanationRepeat + ")" : "") : root.body)
                        textFormat: Text.PlainText
                        color: root.activityStatus === "error" ? "#bd7484"
                            : liveExplanation.narrationShown ? "#82aaff" : "#969bb5"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        wrapMode: liveExplanation.narrationShown ? Text.Wrap : Text.NoWrap
                        elide: liveExplanation.narrationShown ? Text.ElideNone : Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: messageBubble
                objectName: "userMessageBackground"
                visible: !root.activity && root.body.length > 0
                width: Math.min(Math.max(0, parent.width),
                    parent.width * (root.rightAligned ? 0.78 : 0.95), 840,
                    root.body.length > 160 ? 840 : Math.max(140, bubbleMetrics.advanceWidth + 28))
                x: root.rightAligned ? parent.width - width : 0
                implicitHeight: messageBlocks.implicitHeight + 24
                radius: 0
                color: root.userAuthored ? "#20212e" : "transparent"
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
                        objectName: "messageBlockRepeater"
                        // A string-array model resets the editors on every token.
                        // Count-based delegates retain identity and update text in place.
                        model: root.renderedBlocks.length

                        TextEdit {
                            objectName: "messageTextBlock"
                            required property int index
                            width: messageBlocks.width
                            text: root.renderedBlocks[index] || ""
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
                            font.family: "JetBrains Mono"
                            font.pixelSize: 15
                            // Routed through the controller so a non-web scheme in
                            // model output cannot reach the desktop handler.
                            onLinkActivated: link => root.controller.openExternalLink(link, root.linkOriginHost)

                            // Without this the text just looks blue; the I-beam
                            // gives no hint that the URL can be clicked.
                            HoverHandler {
                                objectName: "messageLinkHover"
                                cursorShape: parent.hoveredLink.length > 0
                                    ? Qt.PointingHandCursor : Qt.IBeamCursor
                            }

                            TapHandler {
                                objectName: "messageLinkMenuTap"
                                acceptedButtons: Qt.RightButton
                                onSingleTapped: (eventPoint, button) => {
                                    const link = parent.linkAt(eventPoint.position.x,
                                                               eventPoint.position.y);
                                    if (link.length === 0)
                                        return;
                                    linkMenu.link = link;
                                    linkMenu.originHost = root.linkOriginHost;
                                    linkMenu.x = eventPoint.position.x;
                                    linkMenu.y = eventPoint.position.y;
                                    linkMenu.open();
                                }
                            }

                            Menu {
                                id: linkMenu
                                objectName: "messageLinkMenu"
                                property string link: ""
                                property string originHost: ""
                                MenuItem {
                                    objectName: "messageLinkOpen"
                                    text: qsTr("Open link")
                                    onTriggered: root.controller.openExternalLink(linkMenu.link, linkMenu.originHost)
                                }
                                MenuItem {
                                    objectName: "messageLinkCopy"
                                    text: qsTr("Copy link")
                                    onTriggered: root.controller.copyToClipboard(linkMenu.link)
                                }
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            id: groupToggle
            visible: !root.activity && root.presentedActivityCount > 0 && !root.showTools && !root.forceActivityInline
            activeFocusOnTab: visible
            Accessible.role: Accessible.Button
            Accessible.name: root.groupSummary || root.activitySummary || root.presentedActivityCount + " tool calls"
            function toggle() {
                if (root.groupSummary.length > 0) { root.toggleActivityGroup(); return; }
                if (!root.activityExpanded && root.toolDetailsAvailable)
                    root.controller.loadMessageToolDetails(root.session, root.messageId);
                root.activityExpanded = !root.activityExpanded;
            }
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Space) {
                    groupToggle.toggle(); event.accepted = true;
                }
            }
            Layout.fillWidth: true
            implicitHeight: visible ? 24 : 0
            radius: 0
            color: activityTap.hovered ? "#202335" : "transparent"

            TuiText {
                objectName: "activitySummaryText"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: (root.groupSummary.length > 0
                    ? (root.groupedExpanded ? "Hide · " : "Show · ") + root.groupSummary
                    : (root.activityExpanded ? "Hide · " : "Show · ")
                        + (root.activitySummary || root.presentedActivityCount + " tool calls"))
                color: "#72778f"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }

            HoverHandler { id: activityTap }
            TapHandler {
                onTapped: groupToggle.toggle()
            }
        }

        ColumnLayout {
            visible: !root.activity && root.showActivityCards
                && root.presentedActivityCount > 0
            Layout.fillWidth: true
            Layout.leftMargin: 2
            Layout.rightMargin: 2
            spacing: 1

            TuiButton {
                visible: root.toolDetailsAvailable
                    && root.displayCells.length === 0 && root.tools.length === 0
                text: "Load activity details"
                implicitHeight: 26
                onClicked: root.controller.loadMessageToolDetails(
                    root.session, root.messageId)
            }

            Repeater {
                model: root.showActivityCards ? root.displayCells : []

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
                model: root.showActivityCards ? root.tools : []

                ToolCard {
                    session: root.session
                    required property var modelData
                    visible: Number(modelData._explanationRepeat ?? 1) !== 0
                        && (root.groupSummary.length > 0 || root.displayCells.length === 0
                        || ["Edit", "MultiEdit", "Write"].includes(
                            String(modelData.name || "")))
                    Layout.preferredHeight: visible ? implicitHeight : 0
                    Layout.fillWidth: true
                    tool: modelData
                    controller: root.controller
                    narrator: root.narrator
                    workingDirectory: root.workingDirectory
                    localFilesAllowed: root.localFilesAllowed
                }
            }
        }

        TuiText {
            visible: root.showTimestamp && root.timestamp.length > 0
                && !root.activity
            Layout.alignment: root.rightAligned ? Qt.AlignRight : Qt.AlignLeft
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
            TuiText {
                text: root.deliveryFailed ? "Not delivered" : "Delivering…"
                color: root.deliveryFailed ? "#b56f7c" : "#5f6278"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
            }
            TuiButton {
                visible: root.deliveryFailed
                text: "Retry"
                implicitHeight: 22
                onClicked: root.controller.retryFailedMessage(root.session, root.messageId)
            }
        }
    }
}
