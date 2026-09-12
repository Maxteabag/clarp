pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property var controller
    property bool submitting: false
    signal closeRequested
    color: "#aa08090f"
    function submit() {
        if (submitting || nameField.text.trim().length === 0 || !controller.connected) return;
        submitting = true;
        controller.clearError();
        controller.createAgent(nameField.text.trim(), "~",
            controller.quickStartBackend(), "", "", "", "fresh", "", []);
    }
    onVisibleChanged: {
        if (!visible) return;
        submitting = false;
        nameField.clear();
        controller.clearError();
        Qt.callLater(() => nameField.forceActiveFocus());
    }
    Connections {
        target: root.controller
        function onAgentMutationSucceeded() {
            if (root.visible && root.submitting) root.closeRequested();
        }
        function onErrorMessageChanged() {
            if (root.controller.errorMessage.length > 0) root.submitting = false;
        }
    }
    MouseArea { anchors.fill: parent }
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(480, parent.width - 32)
        height: form.implicitHeight + 40
        radius: 0
        color: "#20212e"
        border.color: "#41445a"
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 14
            TuiText { text: "New contact & chat"; font.pixelSize: 17; color: "#c0caf5" }
            TuiTextField {
                id: nameField
                objectName: "quickNewAgentName"
                Layout.fillWidth: true
                placeholderText: "Name"
                enabled: !root.submitting
                onAccepted: root.submit()
                Keys.onEscapePressed: root.closeRequested()
            }
            TuiText {
                Layout.fillWidth: true
                text: root.controller.quickStartBackend() + " · " + "~"
                color: "#9ca1bd"
                elide: Text.ElideMiddle
                font.pixelSize: 12
            }
            TuiText {
                Layout.fillWidth: true
                visible: root.controller.errorMessage.length > 0 || !root.controller.connected
                text: root.controller.errorMessage || "Connect to the Host to start an agent"
                color: "#e0af68"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Item { Layout.fillWidth: true }
                TuiButton { text: "Cancel"; onClicked: root.closeRequested() }
                TuiButton {
                    text: root.submitting ? "Starting…" : "Create & start · Enter"
                    enabled: !root.submitting && root.controller.connected && nameField.text.trim().length > 0
                    onClicked: root.submit()
                }
            }
        }
    }
}
