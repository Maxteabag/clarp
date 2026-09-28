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
    // A large message is shown as several transcript rows (TranscriptRows):
    // the sender and reply marker go on the first part, the time, tools and
    // delivery state on the last, and copying takes the whole message.
    property int partIndex: 0
    property int partCount: 1
    property string fullBody: body
    readonly property bool firstPart: partIndex === 0
    readonly property bool lastPart: partIndex >= partCount - 1
    readonly property bool replyMarkerVisible: root.firstPart && !root.activity && root.body.length > 0
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
            && root.safeLength(root.displayCells) === 0 && root.safeLength(root.tools) === 0)
            root.controller.loadMessageToolDetails(root.session, root.messageId);
    }
    onForceActivityInlineChanged: Qt.callLater(root.loadInlineDetails)
    onToolDetailsAvailableChanged: Qt.callLater(root.loadInlineDetails)
    onMessageIdChanged: {
        root.groupedExpanded = false;
        root.activityExpanded = false;
        Qt.callLater(root.loadInlineDetails);
    }
    // Reading theme; test stubs without a controller style keep the terminal look.
    readonly property var readingStyle: (controller && controller.readingStyle) || ({})
    function styled(key, fallback) {
        const style = root.readingStyle;
        const value = style ? style[key] : undefined;
        return value === undefined || value === null || value === "" ? fallback : value;
    }
    function safeLength(value) {
        return value && value.length !== undefined ? value.length : 0;
    }
    readonly property string readingFont: String(styled("fontFamily", "JetBrains Mono"))
    readonly property int readingSize: Number(styled("fontPixelSize", 15))
    readonly property int readingMeasure: Number(styled("measure", 840))
    readonly property var narrator: controller.toolNarrator || null
    readonly property bool narrationEnabled: narrator !== null && narrator.enabled
    readonly property bool localFilesAllowed: Boolean(controller.sharedFilesystem)
    readonly property string workingDirectory: {
        controller.agentRevision;
        return narrationEnabled && localFilesAllowed ? controller.agentWorkingDirectory(session) : "";
    }
    readonly property int presentedActivityCount: Math.max(
        root.activityCount, root.safeLength(root.displayCells) + root.safeLength(root.tools))
    readonly property bool showActivityCards: root.groupSummary.length > 0 ? root.groupedExpanded
        : root.showTools || root.forceActivityInline || root.activityExpanded
    readonly property bool userAuthored: !root.groupView && root.authorRole === "user"
        && root.origin !== "agent" && root.origin !== "automation"
    readonly property int mediaRevision: controller.mediaRevision
    // A message can be megabytes: an agent once returned a whole saved web
    // page (3.4 million characters). Parsing and laying that out on the GUI
    // thread froze the window for 46 s when its conversation opened. Past
    // the limit only the start is shown, as plain text; the full text can
    // still be copied.
    readonly property int displayLimit: 100000
    readonly property int oversizedPreview: 16000
    readonly property bool oversized: root.body.length > root.displayLimit
    // What activity rows and tooltips show: the same cap, as one line.
    readonly property string shownBody: root.oversized ? root.body.slice(0, root.oversizedPreview) + "…" : root.body
    readonly property string renderedBody: {
        if (root.oversized)
            return "";
        mediaRevision;
        return controller.resolveMediaMarkdown(body);
    }
    readonly property var renderedBlocks: root.oversized
        ? [root.body.slice(0, root.oversizedPreview)]
        : root.messageKind === "live"
        ? [root.renderedBody]
        : root.controller.markdownDisplayBlocks(root.renderedBody)

    width: ListView.view ? ListView.view.width - ListView.view.leftMargin
        - ListView.view.rightMargin : 600
    visible: activity || body.length > 0 || root.safeLength(displayCells) > 0
        || presentedActivityCount > 0 || (showTools && root.safeLength(tools) > 0)
    implicitHeight: visible ? content.implicitHeight + (activity || body.length === 0 ? 1 : 3) : 0
    objectName: "messageDelegate"

    TextMetrics {
        id: bubbleMetrics
        font.family: root.readingFont
        font.pixelSize: root.readingSize
        // Long messages already use the available width; do not shape the
        // entire growing stream a second time just to measure the bubble.
        text: root.body.length <= 160 ? root.body : ""
    }

    ActivityExplanation {
        session: root.session
        id: liveExplanation
        narrator: root.narrator
        active: root.visible && root.activity && root.toolName.length > 0
        activity: ({name: root.toolName, summary: root.shownBody})
        workingDirectory: root.workingDirectory
        localFilesAllowed: root.localFilesAllowed
    }

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 2

        RowLayout {
            objectName: "groupAuthorLine"
            visible: root.firstPart && (root.groupView || root.teamAuthored) && !root.activity && root.body.length > 0
            Layout.fillWidth: true
            Layout.leftMargin: 2
            spacing: 6
            TuiText {
                objectName: "groupAuthorName"
                text: root.senderName || "Agent"
                color: Theme.accent
                font.pixelSize: 12
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.maximumWidth: Math.max(80, root.width * 0.45)
            }
            TuiText {
                objectName: "groupReplyMarker"
                visible: root.replyToName.length > 0 && root.replyMarkerText.length > 0
                text: root.replyMarkerText
                color: root.styled("mutedText", Theme.secondary)
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
            color: root.styled("mutedText", Theme.secondary)
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
                implicitHeight: Math.max(20, activityRow.implicitHeight + 2)
                radius: Theme.radius
                color: "transparent"
                border.width: 0

                HoverHandler { id: liveActivityHover }
                ToolTip.visible: liveActivityHover.hovered && liveExplanation.text.length > 0
                ToolTip.text: root.toolName + " · " + root.shownBody
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
                        color: root.styled("mutedText", Theme.muted)
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                    TuiText {
                        Layout.fillWidth: true
                        // "thinking Thinking": a summary that only repeats the
                        // label adds nothing.
                        readonly property bool repeatsLabel: root.shownBody.toLowerCase() === String(root.toolName).toLowerCase()
                        text: (root.activityStatus === "error" ? "Error · " : "")
                            + (liveExplanation.narrationShown ? liveExplanation.displayText + (root.explanationRepeat > 1 ? " (x" + root.explanationRepeat + ")" : "")
                               : repeatsLabel ? "" : root.shownBody)
                        textFormat: Text.PlainText
                        color: root.activityStatus === "error" ? Theme.danger
                            : liveExplanation.narrationShown ? root.styled("link", Theme.link)
                            : root.styled("mutedText", Theme.muted)
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
                    parent.width * (root.rightAligned ? 0.78 : 0.95), root.readingMeasure,
                    root.body.length > 160 ? root.readingMeasure : Math.max(140, bubbleMetrics.advanceWidth + 24))
                x: root.rightAligned ? parent.width - width : 0
                implicitHeight: messageBlocks.implicitHeight + 16
                radius: Theme.radius
                color: root.userAuthored ? root.styled("bubble", Theme.raised) : "transparent"
                border.width: root.deliveryFailed ? 1 : 0
                border.color: Theme.danger
                opacity: root.pending ? 0.68 : 1

                Column {
                    id: messageBlocks
                    x: 10
                    y: 8
                    width: parent.width - 20
                    spacing: 6

                    Repeater {
                        objectName: "messageBlockRepeater"
                        // A string-array model resets the editors on every token.
                        // Count-based delegates retain identity and update text in place.
                        model: root.renderedBlocks.length

                        TextEdit {
                            objectName: "messageTextBlock"
                            required property int index
                            width: messageBlocks.width
                            // Qt's Markdown parser is not incremental-safe when a
                            // stream ends halfway through a fence/list/tag. Present
                            // growing text plainly; the finalized row upgrades to
                            // pre-styled rich text without changing model identity.
                            // Styling happens before layout: restyling after the
                            // row is placed changes its height and makes the list
                            // jump while scrolling up.
                            readonly property string block: root.renderedBlocks[index] || ""
                            readonly property string wantedText: root.oversized || root.messageKind === "live" || !root.controller.styledMarkdownHtml
                                ? block
                                : root.controller.styledMarkdownHtml(block, {
                                    bodyPixelSize: root.readingSize, bodyFamily: root.readingFont,
                                    monoFamily: "JetBrains Mono", codeBackground: String(Theme.control),
                                    quoteText: String(Theme.muted), link: String(Theme.link), rule: String(Theme.rule)
                                })
                            readonly property int wantedFormat: root.oversized ? Text.PlainText
                                : root.messageKind === "live" || !root.controller.styledMarkdownHtml
                                ? (root.messageKind === "live" ? Text.PlainText : Text.MarkdownText) : Text.RichText
                            // Text and format are applied in this order, never by two
                            // bindings: switching a TextEdit from rich to plain text
                            // while the rich document is loaded turns that document's
                            // HTML source into the plain text (a reused row showed a
                            // message as '<!DOCTYPE HTML PUBLIC ...').
                            function applyContent() {
                                if (textFormat !== wantedFormat) {
                                    text = "";
                                    textFormat = wantedFormat;
                                }
                                if (text !== wantedText) text = wantedText;
                            }
                            onWantedTextChanged: applyContent()
                            onWantedFormatChanged: applyContent()
                            Component.onCompleted: applyContent()
                            readOnly: true
                            selectByMouse: true
                            persistentSelection: true
                            wrapMode: Text.Wrap
                            color: root.styled("text", Theme.body)
                            selectedTextColor: root.styled("selectedText", Theme.selectedText)
                            selectionColor: root.styled("selection", Theme.selection)
                            font.family: root.readingFont
                            font.pixelSize: root.readingSize
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
                    Row {
                        objectName: "oversizedMessageNote"
                        visible: root.oversized
                        spacing: 10

                        TuiText {
                            anchors.verticalCenter: parent.verticalCenter
                            text: qsTr("Showing the first %1 of %2 characters.")
                                .arg(root.oversizedPreview.toLocaleString(Qt.locale(), "f", 0))
                                .arg(root.body.length.toLocaleString(Qt.locale(), "f", 0))
                            color: Theme.muted
                            font.pixelSize: 12
                        }
                        TuiButton {
                            objectName: "copyFullMessage"
                            text: qsTr("Copy full message")
                            implicitHeight: 26
                            onClicked: root.controller.copyToClipboard(root.fullBody)
                        }
                    }
                    TuiButton {
                        objectName: "copySplitMessage"
                        visible: root.partCount > 1 && root.lastPart
                        text: qsTr("Copy full message")
                        implicitHeight: 26
                        onClicked: root.controller.copyToClipboard(root.fullBody)
                    }
                }
            }
        }

        Rectangle {
            id: groupToggle
            visible: root.lastPart && !root.activity && root.presentedActivityCount > 0 && !root.showTools && !root.forceActivityInline
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
            implicitHeight: visible ? 20 : 0
            radius: Theme.radius
            color: activityTap.hovered ? Theme.hover : "transparent"

            TuiText {
                objectName: "activitySummaryText"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: (root.groupSummary.length > 0
                    ? (root.groupedExpanded ? "Hide · " : "Show · ") + root.groupSummary
                    : (root.activityExpanded ? "Hide · " : "Show · ")
                        + (root.activitySummary || root.presentedActivityCount + " tool calls"))
                color: root.styled("faintText", Theme.faint)
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }

            HoverHandler { id: activityTap }
            TapHandler {
                onTapped: groupToggle.toggle()
            }
        }

        ColumnLayout {
            visible: root.lastPart && !root.activity && root.showActivityCards
                && root.presentedActivityCount > 0
            Layout.fillWidth: true
            Layout.leftMargin: 2
            Layout.rightMargin: 2
            spacing: 1

            TuiButton {
                visible: root.toolDetailsAvailable
                    && root.safeLength(root.displayCells) === 0 && root.safeLength(root.tools) === 0
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
                    motionClock: root.controller.avatarMotion
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
                        && (root.groupSummary.length > 0 || root.safeLength(root.displayCells) === 0
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
            visible: root.lastPart && root.showTimestamp && root.timestamp.length > 0
                && !root.activity
            Layout.alignment: root.rightAligned ? Qt.AlignRight : Qt.AlignLeft
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            text: Qt.formatDateTime(new Date(root.timestamp), "MMM d  HH:mm")
            color: root.styled("faintText", Theme.faint)
            font.family: "JetBrains Mono"
            font.pixelSize: 9
        }

        RowLayout {
            visible: root.lastPart && (root.pending || root.deliveryFailed)
            Layout.alignment: root.userAuthored ? Qt.AlignRight : Qt.AlignLeft
            Layout.leftMargin: 4
            Layout.rightMargin: 4
            spacing: 6
            TuiText {
                text: root.deliveryFailed ? "Not delivered" : "Delivering…"
                color: root.deliveryFailed ? Theme.danger : Theme.faint
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
