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

    color: active ? "#292b3a" : "#20212e"
    border.color: active ? "#bb9af7" : "#302b37"
    border.width: active ? 2 : 1
    radius: 2
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
        // Stay passive until a click is recognized. Dragging the scroll thumb
        // or selecting message text must not grab the pointer/focus the composer.
        gesturePolicy: TapHandler.DragThreshold
        onTapped: eventPoint => {
            // The composer handles its own taps. Everywhere else, selecting a
            // pane means it is immediately ready to receive text.
            if (root.active && eventPoint.position.y >= root.height - 54)
                return;
            root.controller.panes.focusPane(String(root.node.id));
            root.controller.requestComposerFocus(String(root.node.id));
        }
    }
}
