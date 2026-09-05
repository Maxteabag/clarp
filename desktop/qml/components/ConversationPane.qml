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
    signal openConnection
    signal queueRequested(string session)
    signal profileRequested(string session)

    color: root.active ? "#1d2132" : "#151720"

    Behavior on color {
        ColorAnimation { duration: 120 }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            color: root.active ? "#24283d" : "#191b26"

            HoverHandler { id: headerHover }

            Behavior on color {
                ColorAnimation { duration: 120 }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 11
                anchors.rightMargin: 8
                spacing: 9

                AgentAvatar {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                    controller: root.controller
                    session: root.session
                    name: root.controller.agentName(root.session)
                    avatarSize: 24
                    cornerRadius: 6
                    fallbackColor: root.active ? "#555970" : "#3b3e50"
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1

                    Text {
                        Layout.fillWidth: true
                        text: {
                            root.agentRevision;
                            return root.controller.agentName(root.session) || "No agent selected";
                        }
                        color: root.active ? "#c9cbdc" : "#8b8ea5"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignLeft
                        elide: Text.ElideRight
                    }
                    Text {
                        text: {
                            root.agentRevision;
                            return root.session.length > 0
                                ? root.controller.agentBackend(root.session)
                                : root.controller.baseUrl;
                        }
                        visible: root.active && root.width > 520
                        color: "#5e6177"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 9
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                StatusPill {
                    status: {
                        root.agentRevision;
                        return root.controller.agentState(root.session) || root.controller.connectionState;
                    }
                }

                ToolButton {
                    id: paneMenuButton
                    visible: root.active && root.session.length > 0
                        && (headerHover.hovered || paneMenu.visible)
                    text: "···"
                    implicitWidth: 28
                    implicitHeight: 26
                    onClicked: paneMenu.open()
                    Menu {
                        id: paneMenu
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
                            text: "Open terminal"
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

                Text {
                    Layout.fillWidth: true
                    text: root.controller.errorMessage || root.conversationModel.error
                    color: "#c9959e"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
                Button {
                    visible: root.conversationModel.error.length > 0
                    text: "Retry"
                    implicitHeight: 26
                    onClicked: root.controller.refreshSession(root.session)
                }
                ToolButton {
                    text: "×"
                    onClicked: {
                        root.controller.clearError();
                        root.conversationModel.error = "";
                    }
                }
            }
        }

        ListView {
            id: transcript
            property bool followLatest: true
            property bool newMessagesBelow: false
            property bool userInteracting: false
            property real heightBeforePrepend: -1
            readonly property real distanceFromBottom: Math.max(
                0, contentHeight - height - Math.max(0, contentY))

            function scrollToLatest() {
                followLatest = true;
                newMessagesBelow = false;
                Qt.callLater(() => positionViewAtEnd());
            }

            Layout.fillWidth: true
            Layout.fillHeight: true
            model: root.conversationModel
            clip: true
            spacing: 4
            leftMargin: 14
            rightMargin: 14
            topMargin: 10
            bottomMargin: 10
            reuseItems: true
            boundsBehavior: Flickable.StopAtBounds
            onMovementStarted: userInteracting = true
            onContentYChanged: {
                if (userInteracting && distanceFromBottom > 32)
                    followLatest = false;
            }
            onMovementEnded: {
                userInteracting = false;
                if (distanceFromBottom <= 32) {
                    followLatest = true;
                    newMessagesBelow = false;
                }
            }
            onContentHeightChanged: {
                if (followLatest && !userInteracting)
                    Qt.callLater(() => positionViewAtEnd());
            }
            Component.onCompleted: scrollToLatest()

            header: Item {
                width: transcript.width
                height: root.conversationModel.hasMore ? 32 : 4

                Button {
                    visible: root.conversationModel.hasMore
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.conversationModel.loading ? "Loading…" : "Load earlier messages"
                    enabled: !root.conversationModel.loading
                    onClicked: {
                        transcript.followLatest = false;
                        transcript.heightBeforePrepend = transcript.contentHeight;
                        root.controller.loadOlderSession(root.session);
                    }
                }
            }

            delegate: MessageDelegate {
                controller: root.controller
                session: root.session
                showTools: root.controller.toolsVisible
                showTimestamp: root.controller.timestampsVisible
            }

            footer: Item {
                width: transcript.width
                height: root.conversationModel.loading ? 34 : 6

                BusyIndicator {
                    anchors.centerIn: parent
                    running: root.conversationModel.loading
                    visible: running
                    implicitWidth: 22
                    implicitHeight: 22
                }
            }

            ScrollBar.vertical: ScrollBar {}

            Connections {
                target: root.conversationModel
                function onRowsAppended(fromCurrentUser) {
                    if (fromCurrentUser || transcript.followLatest
                        || transcript.distanceFromBottom <= 32) {
                        transcript.scrollToLatest();
                    } else {
                        transcript.newMessagesBelow = true;
                    }
                }
                function onRowsPrepended() {
                    const previous = transcript.heightBeforePrepend;
                    transcript.heightBeforePrepend = -1;
                    if (previous < 0)
                        return;
                    Qt.callLater(() => {
                        transcript.contentY += Math.max(0, transcript.contentHeight - previous);
                    });
                }
            }

            Label {
                anchors.centerIn: parent
                visible: transcript.count === 0 && !root.conversationModel.loading
                text: root.session.length > 0 ? "No messages yet. Start the conversation below." : "Choose an agent from the sidebar."
                color: "#5e6176"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
        }

        Composer {
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
            controller: root.controller
            session: root.session
            paneId: root.paneId
            active: root.active
            onOpenConnection: root.openConnection()
        }
    }

    onSessionChanged: {
        if (session.length > 0 && controller.connected)
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

    ToolButton {
        visible: !transcript.followLatest
            && (transcript.newMessagesBelow || transcript.distanceFromBottom >= 180)
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 12
        anchors.bottomMargin: 64
        width: 34
        height: 34
        z: 30
        text: transcript.newMessagesBelow ? "↓•" : "↓"
        onClicked: transcript.scrollToLatest()
        ToolTip.visible: hovered
        ToolTip.text: transcript.newMessagesBelow ? "Jump to new messages" : "Jump to latest"
        background: Rectangle {
            radius: 17
            color: "#30354f"
            border.color: "#777fae"
        }
    }
}
