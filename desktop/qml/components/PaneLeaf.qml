import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property var node
    signal openConnectionRequested
    readonly property bool active: controller.panes.activePaneId === String(node.id)
    readonly property string session: String(node.session || "")

    color: "#121116"
    border.color: active ? "#6f527b" : "#26222c"
    border.width: active ? 2 : 1

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.active ? 2 : 1
        spacing: 0

        Rectangle {
            visible: root.controller.panes.paneCount > 1
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 34 : 0
            color: root.active ? "#201b25" : "#17151c"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 5
                spacing: 5

                Text {
                    Layout.fillWidth: true
                    text: root.controller.agentName(root.session)
                    color: root.active ? "#d8cbdc" : "#756f7c"
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                ToolButton {
                    text: "V"
                    onClicked: {
                        root.controller.panes.focusPane(String(root.node.id));
                        root.controller.panes.splitActive("vertical", root.session);
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: "Split side by side"
                }
                ToolButton {
                    text: "H"
                    onClicked: {
                        root.controller.panes.focusPane(String(root.node.id));
                        root.controller.panes.splitActive("horizontal", root.session);
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: "Split above and below"
                }
                ToolButton {
                    text: "Z"
                    onClicked: {
                        root.controller.panes.focusPane(String(root.node.id));
                        root.controller.panes.toggleZoom();
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: "Zoom pane"
                }
                ToolButton {
                    text: "×"
                    onClicked: root.controller.panes.closePane(String(root.node.id))
                    ToolTip.visible: hovered
                    ToolTip.text: "Close pane"
                }
            }
        }

        ConversationPane {
            Layout.fillWidth: true
            Layout.fillHeight: true
            controller: root.controller
            session: root.session
            conversationModel: root.controller.conversationForSession(root.session)
            onOpenConnection: root.openConnectionRequested()
        }
    }

    TapHandler {
        acceptedButtons: Qt.LeftButton
        gesturePolicy: TapHandler.WithinBounds
        onTapped: root.controller.panes.focusPane(String(root.node.id))
    }
}
