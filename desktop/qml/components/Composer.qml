pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    required property string paneId
    required property bool active
    property bool dropActive: false
    readonly property int agentRevision: controller.agentRevision
    readonly property int composerRevision: controller.composerRevision
    readonly property int queueCount: {
        agentRevision;
        return controller.agentQueueCount(session);
    }
    readonly property var attachments: {
        composerRevision;
        return controller.composerAttachments(paneId, session);
    }
    readonly property bool canSend: {
        composerRevision;
        return controller.composerCanSend(paneId, session);
    }
    readonly property string draftScope: paneId + "|" + session
    signal openConnection

    implicitHeight: 46 + (queueCount > 0 ? 25 : 0) + (attachments.length > 0 ? 31 : 0)
    color: root.active ? "#191a23" : "#151720"
    border.color: root.active ? "#4f5577" : "#272a39"
    border.width: 1

    function restoreFocus() {
        if (root.visible && root.active && root.controller.composerFocusPane === root.paneId)
            Qt.callLater(() => editor.forceActiveFocus());
    }

    function restoreDraft() {
        editor.text = root.controller.paneDraft(root.paneId, root.session);
    }

    onVisibleChanged: restoreFocus()
    onActiveChanged: {
        restoreDraft();
        restoreFocus();
    }
    onDraftScopeChanged: Qt.callLater(() => root.restoreDraft())
    Component.onCompleted: {
        restoreDraft();
        restoreFocus();
    }

    Connections {
        target: root.controller
        function onComposerFocusPaneChanged() {
            root.restoreFocus();
        }
        function onDraftChanged(session, text, originPaneId) {
            if (session === root.session && originPaneId !== root.paneId
                    && editor.text !== text)
                editor.text = text;
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 6

        Text {
            visible: root.queueCount > 0
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 19 : 0
            text: root.queueCount + (root.queueCount === 1 ? " message queued" : " messages queued")
                + "  ·  Ctrl+Enter queues next"
            color: "#8f93b3"
            font.family: "JetBrains Mono"
            font.pixelSize: 8
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
        }

        Flickable {
            visible: root.attachments.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 25 : 0
            contentWidth: attachmentRow.implicitWidth
            contentHeight: height
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: attachmentRow
                height: parent.height
                spacing: 5
                Repeater {
                    model: root.attachments
                    delegate: Rectangle {
                        id: attachmentChip
                        required property var modelData
                        height: 24
                        width: Math.min(220, attachmentLabel.implicitWidth + 36)
                        radius: 4
                        color: "#262938"
                        border.color: "#42465e"
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 7
                            spacing: 3
                            Image {
                                visible: String(attachmentChip.modelData.content_type || "")
                                    .startsWith("image/")
                                Layout.preferredWidth: visible ? 18 : 0
                                Layout.preferredHeight: visible ? 18 : 0
                                source: {
                                    const local = String(attachmentChip.modelData.local_source
                                        || (attachmentChip.modelData.local
                                            ? attachmentChip.modelData.path : "") || "");
                                    return local.length > 0 ? "file://" + local : "";
                                }
                                fillMode: Image.PreserveAspectCrop
                                asynchronous: true
                            }
                            Text {
                                id: attachmentLabel
                                Layout.fillWidth: true
                                text: String(attachmentChip.modelData.name || "file")
                                    + (String(attachmentChip.modelData.status || "ready") === "ready"
                                        ? "" : " · " + String(attachmentChip.modelData.status))
                                color: String(attachmentChip.modelData.status || "ready") === "failed"
                                    ? "#c98a98" : "#adb1c8"
                                font.pixelSize: 9
                                elide: Text.ElideMiddle
                            }
                            ToolButton {
                                text: "×"
                                visible: root.active
                                implicitWidth: 22
                                implicitHeight: 22
                                onClicked: root.controller.removeComposerAttachment(
                                    root.paneId, root.session,
                                    String(attachmentChip.modelData.id || ""))
                            }
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 6

        ToolButton {
            visible: root.active
            text: "+"
            enabled: root.session.length > 0
            implicitWidth: 28
            implicitHeight: 28
            onClicked: fileDialog.open()
            ToolTip.visible: hovered
            ToolTip.text: "Attach file"
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 4
            color: "#1d1e29"
                border.color: editor.activeFocus ? "#8f96c5" : (root.active ? "#3a3e55" : "#272a38")
            border.width: 1

            Text {
                anchors.left: parent.left
                anchors.leftMargin: 9
                anchors.verticalCenter: parent.verticalCenter
                text: "›"
                color: editor.activeFocus ? "#a7aac1" : "#676a80"
                font.family: "JetBrains Mono"
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }

            TextArea {
                id: editor
                objectName: "paneComposerEditor"
                anchors.fill: parent
                anchors.margins: 2
                anchors.leftMargin: 17
                text: ""
                placeholderText: root.session.length > 0 ? "Message " + root.controller.agentName(root.session) : "Choose an agent"
                enabled: root.active && root.session.length > 0
                opacity: root.active ? 1 : 0.58
                wrapMode: TextArea.Wrap
                color: "#c7c9dc"
                placeholderTextColor: "#55586c"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
                background: null
                leftPadding: 7
                topPadding: 6
                bottomPadding: 5
                onTextChanged: root.controller.setPaneDraft(root.paneId, root.session, text)
                onActiveFocusChanged: {
                    if (activeFocus)
                        root.controller.requestComposerFocus(root.paneId);
                }

                Keys.onPressed: event => {
                    const shift = (event.modifiers & Qt.ShiftModifier) !== 0;
                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !shift) {
                        if (editor.text.trim().length > 0 || root.attachments.length > 0) {
                            const queue = (event.modifiers & Qt.ControlModifier) !== 0;
                            const sent = root.controller.sendComposerMessage(
                                root.paneId, root.session, editor.text, queue);
                            if (sent)
                                editor.clear();
                        }
                        event.accepted = true;
                    }
                }
            }
        }

        ToolButton {
            id: stopButton
            visible: {
                if (!root.active)
                    return false;
                root.agentRevision;
                const state = root.controller.agentState(root.session);
                return state === "thinking" || state === "tool" || state === "compacting";
            }
            text: "■"
            implicitWidth: visible ? 28 : 0
            implicitHeight: 28
            onClicked: root.controller.stopSession(root.session)
            ToolTip.visible: hovered
            ToolTip.text: "Stop agent · Ctrl+."
            contentItem: Text {
                text: stopButton.text
                color: "#9e7d87"
                font.pixelSize: 9
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 4
                color: stopButton.hovered ? "#242633" : "transparent"
            }
        }

        ToolButton {
            id: playbackButton
            visible: root.active && (root.controller.audio.playing || root.controller.audio.paused)
            text: "■"
            implicitWidth: visible ? 28 : 0
            implicitHeight: 28
            onClicked: root.controller.audio.silence()
            ToolTip.visible: hovered
            ToolTip.text: "Stop voice playback"
            contentItem: Text {
                text: playbackButton.text
                color: "#74778e"
                font.pixelSize: 8
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 4
                color: playbackButton.hovered ? "#242633" : "transparent"
            }
        }

        ToolButton {
            id: recordButton
            visible: root.active
            text: root.controller.audio.transcribing ? "…" : root.controller.audio.recording ? "■" : "●"
            enabled: !root.controller.audio.transcribing && root.session.length > 0
            implicitWidth: 28
            implicitHeight: 28
            onClicked: root.controller.toggleRecordingForSession(root.session)
            ToolTip.visible: hovered
            ToolTip.text: root.controller.audio.recording ? "Stop and transcribe" : "Talk · Ctrl+Shift+Space"
            contentItem: Text {
                text: recordButton.text
                color: root.controller.audio.recording ? "#b98591" : "#74778e"
                font.pixelSize: 10
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 4
                color: recordButton.hovered ? "#242633" : "transparent"
            }
        }
        }
    }

    FileDialog {
        id: fileDialog
        title: "Attach a file"
        fileMode: FileDialog.OpenFiles
        onAccepted: {
            for (const file of selectedFiles)
                root.controller.attachLocalFile(root.paneId, root.session, file);
        }
    }

    DropArea {
        anchors.fill: parent
        enabled: root.active && root.session.length > 0
        onEntered: drag => {
            root.dropActive = drag.hasUrls;
            drag.accepted = drag.hasUrls;
        }
        onExited: root.dropActive = false
        onDropped: drop => {
            root.dropActive = false;
            for (const url of drop.urls)
                root.controller.attachLocalFile(root.paneId, root.session, url);
            drop.acceptProposedAction();
        }
    }

    Rectangle {
        anchors.fill: parent
        visible: root.dropActive
        z: 40
        radius: 4
        color: "#d0262a3d"
        border.color: "#a7addb"
        border.width: 2
        Text {
            anchors.centerIn: parent
            text: "DROP TO ATTACH"
            color: "#d8dbef"
            font.family: "JetBrains Mono"
            font.pixelSize: 10
            font.weight: Font.DemiBold
        }
    }

    MouseArea {
        anchors.fill: parent
        visible: !root.active
        cursorShape: Qt.IBeamCursor
        onClicked: {
            root.controller.panes.focusPane(root.paneId);
            root.controller.requestComposerFocus(root.paneId);
        }
    }
}
