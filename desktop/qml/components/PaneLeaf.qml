import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property var node
    signal openConnectionRequested
    signal queueRequested(string session)
    signal profileRequested(string session)
    readonly property bool active: controller.panes.activePaneId === String(node.id)
    readonly property string session: String(node.session || "")
    focus: true

    color: active ? "#23273b" : "#171923"
    border.color: active ? "#a7addb" : "#303347"
    border.width: active ? 2 : 1
    radius: 5
    clip: true
    opacity: active ? 1 : 0.76

    Behavior on opacity {
        NumberAnimation { duration: 120 }
    }
    Behavior on color {
        ColorAnimation { duration: 120 }
    }
    Behavior on border.color {
        ColorAnimation { duration: 120 }
    }

    Rectangle {
        visible: root.active
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 3
        color: "#bbc0ed"
        z: 3
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.active ? 2 : 1
        spacing: 0

        ConversationPane {
            Layout.fillWidth: true
            Layout.fillHeight: true
            controller: root.controller
            session: root.session
            paneId: String(root.node.id)
            active: root.active
            conversationModel: root.controller.conversationForSession(root.session)
            onOpenConnection: root.openConnectionRequested()
            onQueueRequested: session => root.queueRequested(session)
            onProfileRequested: session => root.profileRequested(session)
        }
    }

    TapHandler {
        acceptedButtons: Qt.LeftButton
        gesturePolicy: TapHandler.WithinBounds
        onTapped: eventPoint => {
            // The active composer occupies the bottom 46 px. Its TextArea
            // keeps its own focus; taps anywhere else explicitly enter pane
            // focus so subsequent typing cannot leak into a hidden caret.
            if (root.active && eventPoint.position.y >= root.height - 46)
                return;
            root.controller.requestComposerFocus("");
            root.controller.panes.focusPane(String(root.node.id));
            root.forceActiveFocus();
        }
    }
}
