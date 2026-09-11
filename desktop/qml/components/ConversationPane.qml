pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    required property string paneId
    required property var conversationModel
    required property bool active
    readonly property int agentRevision: controller.agentRevision
    // Agent-to-agent pair rooms are read-only Host projections.
    readonly property bool pairRoom: root.controller.isPairSession(root.session)
    readonly property var pairRoomInfo: {
        root.controller.agentConversations;
        return root.pairRoom ? root.controller.agentConversation(root.session) : ({});
    }
    readonly property var pairParticipants: (root.pairRoomInfo && root.pairRoomInfo.participants) || []
    function jumpToLatest() { transcript.scrollToLatest(); }
    signal openConnection
    signal queueRequested(string session)
    signal profileRequested(string session)

    readonly property bool working: root.controller.showWhenReady
        && ["thinking", "tool", "compacting", "running"].includes(root.currentAgentState)
    readonly property string currentAgentState: root.agentRevision >= 0
        ? root.controller.agentState(root.session) : ""
    onConversationModelChanged: Qt.callLater(() => transcript.scrollToLatest())
    Connections {
        target: root.conversationModel
        function onConversationIdChanged() { transcript.scrollToLatest(); }
    }
    ConversationPresentationModel {
        id: presentation
        sourceModel: root.conversationModel
        showWhenReady: root.controller.showWhenReady
        activityMode: root.controller.activityDisplayMode
        function refreshExplanations() {
            const narrator = root.controller.toolNarrator || null;
            updateExplanations(narrator, root.session,
                root.controller.sharedFilesystem ? root.controller.agentWorkingDirectory(root.session) : "",
                Boolean(root.controller.sharedFilesystem));
        }
        Component.onCompleted: refreshExplanations()
        property Connections explanationUpdates: Connections {
            target: root.controller.toolNarrator || null
            function onChanged() { Qt.callLater(presentation.refreshExplanations); }
            function onEnabledChanged() { Qt.callLater(presentation.refreshExplanations); }
        }
    }

    color: root.active ? "#1a1b26" : "#1a1b26"

    Behavior on color {
        ColorAnimation { duration: 120 }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            color: "#1a1b26"

            HoverHandler { id: headerHover }

            Behavior on color {
                ColorAnimation { duration: 120 }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 11
                anchors.rightMargin: 8
                spacing: 9

                HeaderContext {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    agentName: {
                        root.agentRevision;
                        return root.controller.agentName(root.session);
                    }
                    metadata: {
                        root.agentRevision;
                        root.controller.sharedFilesystem;
                        return root.controller.agentDetails(root.session);
                    }
                    showRuntime: root.session.length > 0 && !root.pairRoom
                }

                TuiToolButton {
                    id: paneMenuButton
                    visible: root.active && root.session.length > 0 && !root.pairRoom
                        && (headerHover.hovered || paneMenu.visible)
                    text: "More"
                    display: AbstractButton.IconOnly
                    icon.source: Qt.resolvedUrl("../../resources/icons/more.svg")
                    icon.width: 18
                    icon.height: 18
                    icon.color: "#a6adc8"
                    Accessible.name: "Conversation actions"
                    ToolTip.visible: hovered
                    ToolTip.text: "More"
                    implicitWidth: 28
                    implicitHeight: 26
                    onClicked: paneMenu.open()
                    Menu {
                        id: paneMenu
                        MenuItem {
                            text: "Show when ready"
                            checkable: true
                            checked: root.controller.showWhenReady
                            onTriggered: root.controller.showWhenReady = !root.controller.showWhenReady
                        }
                        MenuItem {
                            text: "Queued messages"
                            onTriggered: root.queueRequested(root.session)
                        }
                        MenuItem {
                            text: root.controller.timestampsVisible
                                ? "Hide timestamps" : "Show timestamps"
                            onTriggered: root.controller.timestampsVisible =
                                !root.controller.timestampsVisible
                        }
                        MenuItem {
                            text: root.controller.toolsVisible
                                ? "Collapse tool details" : "Expand tool details"
                            onTriggered: root.controller.toolsVisible =
                                !root.controller.toolsVisible
                        }
                        MenuSeparator {}
                        MenuItem {
                            text: "Open files"
                            onTriggered: root.controller.openAgentFiles(root.session)
                        }
                        MenuItem {
                            text: "Open agent in terminal"
                            onTriggered: root.controller.openAgentTerminal(root.session)
                        }
                        MenuItem {
                            text: "Agent profile"
                            onTriggered: root.profileRequested(root.session)
                        }
                    }
                }

            }
        }

        Rectangle {
            visible: root.active && (root.controller.errorMessage.length > 0
                || root.conversationModel.error.length > 0)
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 38 : 0
            color: "#2b2028"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 8

                TuiText {
                    Layout.fillWidth: true
                    text: root.controller.errorMessage || root.conversationModel.error
                    color: "#c9959e"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
                TuiButton {
                    visible: root.conversationModel.error.length > 0
                    text: "Retry"
                    implicitHeight: 26
                    onClicked: root.controller.refreshSession(root.session)
                }
                TuiToolButton {
                    text: "Dismiss · Esc"
                    onClicked: {
                        root.controller.clearError();
                        root.conversationModel.error = "";
                    }
                }
            }
        }

        TranscriptList {
            id: transcript

            Layout.fillWidth: true
            Layout.fillHeight: true
            model: presentation
            clip: true
            spacing: 2
            leftMargin: 14
            rightMargin: 14
            topMargin: 10
            bottomMargin: 10
            reuseItems: true
            section.property: "dayLabel"
            section.delegate: Item {
                required property string section
                width: transcript.width
                readonly property bool showHeading: section.length > 0
                    && !(section === "Today" && presentation.leadingDayLabel === "Today")
                height: showHeading ? 32 : 0
                visible: showHeading
                Rectangle {
                    anchors.left: parent.left
                    anchors.leftMargin: 14
                    anchors.right: dateLabel.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    height: 1
                    color: "#303342"
                }
                TuiText {
                    id: dateLabel
                    anchors.centerIn: parent
                    text: parent.section
                    color: "#8d93b0"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
                }
                Rectangle {
                    anchors.left: dateLabel.right
                    anchors.leftMargin: 12
                    anchors.right: parent.right
                    anchors.rightMargin: 14
                    anchors.verticalCenter: parent.verticalCenter
                    height: 1
                    color: "#303342"
                }
            }
            boundsBehavior: Flickable.StopAtBounds

            header: Item {
                width: transcript.width
                height: root.conversationModel.hasMore ? 32 : 4

                TuiButton {
                    visible: root.conversationModel.hasMore
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.conversationModel.loading ? "Loading…" : "Load earlier messages"
                    enabled: !root.conversationModel.loading
                    onClicked: {
                        transcript.pauseFollowing();
                        root.controller.loadOlderSession(root.session);
                    }
                }
            }

            delegate: MessageDelegate {
                required property var model
                senderAgentId: String(model.senderAgentId || "")
                senderSession: String(model.senderSession || "")
                replyToAgentId: String(model.replyToAgentId || "")
                replyToName: String(model.replyToName || "")
                replyToSession: String(model.replyToSession || "")
                delivery: String(model.delivery || "")
                explanationRepeat: Number(model.explanationRepeat || 1)
                groupView: root.pairRoom
                activitySummary: String(model.activityLabel || "")
                required property var groupIds
                required property string groupLabel
                required property bool groupExpanded
                required property bool activityInline
                groupSummary: groupLabel
                groupedExpanded: groupExpanded
                forceActivityInline: activityInline
                onToggleActivityGroup: {
                    if (!groupExpanded) for (const id of groupIds)
                        root.controller.loadMessageToolDetails(root.session, id);
                    presentation.toggleGroup(messageId);
                }
                controller: root.controller
                session: root.session
                showTools: root.controller.toolsVisible
                showTimestamp: root.controller.timestampsVisible
            }

            footer: Item {
                width: transcript.width
                height: root.working ? 46 : root.conversationModel.loading ? 34 : 6

                TypingIndicator {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    visible: root.working
                }
                TuiBusyIndicator {
                    anchors.centerIn: parent
                    running: root.conversationModel.loading && !root.working
                    visible: running
                    implicitWidth: 22
                    implicitHeight: 22
                }
            }

            Connections {
                target: presentation
                function onRowsAppended(fromCurrentUser) {
                    if (fromCurrentUser || (transcript.followLatest && !transcript.userInteracting)) {
                        transcript.scrollToLatest();
                    } else {
                        transcript.newMessagesBelow = true;
                    }
                }
            }

            TuiLabel {
                anchors.centerIn: parent
                visible: root.session.length === 0 && transcript.count === 0 && !root.conversationModel.loading
                text: "Choose an agent · Ctrl+K"
                color: "#5e6176"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
        }

        Composer {
            visible: !root.pairRoom
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? implicitHeight : 0
            controller: root.controller
            session: root.session
            paneId: root.paneId
            active: root.active
            onOpenConnection: root.openConnection()
        }

        Rectangle {
            objectName: "pairRoomFooter"
            visible: root.pairRoom
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 44 : 0
            color: "#171822"
            TuiText {
                anchors.centerIn: parent
                width: parent.width - 32
                horizontalAlignment: Text.AlignHCenter
                text: "Agents talk here. To reply, open one of them and message it directly."
                color: "#8d93b0"
                font.pixelSize: 12
                elide: Text.ElideRight
            }
        }
    }

    onSessionChanged: {
        presentation.refreshExplanations();
        presentation.beginVisit();
        if (session.length > 0 && controller.connected && !root.pairRoom)
            controller.loadMedia(session);
    }
    Component.onCompleted: {
        if (session.length > 0 && controller.connected)
            controller.loadMedia(session);
    }
    Connections {
        target: root.controller
        function onConnectedChanged() {
            if (root.controller.connected && root.session.length > 0)
                root.controller.loadMedia(root.session);
        }
    }

    TuiToolButton {
        objectName: "jumpToLatestButton"
        visible: !transcript.followLatest
            && (transcript.newMessagesBelow || transcript.distanceFromBottom >= 180)
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 12
        anchors.bottomMargin: 64
        width: 104
        height: 28
        z: 30
        text: transcript.newMessagesBelow ? "New messages" : "Latest"
        onClicked: transcript.scrollToLatest()
        ToolTip.visible: hovered
        ToolTip.text: "Jump to latest · Ctrl+End"
        background: Rectangle {
            radius: 0
            color: "#30354f"
            border.color: "#777fae"
        }
    }
}
