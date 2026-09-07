pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property var controller
    // The chat being renamed. Captured when the dialog opens rather than
    // followed live, so switching panes underneath cannot retarget a rename
    // the user already started typing.
    property string session: ""
    property string currentName: ""
    property bool submitting: false
    signal closeRequested
    color: "#aa08090f"
    function submit() {
        const value = nameField.text.trim();
        if (submitting || value.length === 0 || !controller.connected) return;
        if (value === root.currentName) { root.closeRequested(); return; }
        submitting = true;
        controller.clearError();
        controller.renameAgent(root.session, value);
    }
    function open(session, name) {
        root.session = session;
        root.currentName = name;
        root.visible = true;
    }
    onVisibleChanged: {
        if (!visible) return;
        submitting = false;
        nameField.text = root.currentName;
        controller.clearError();
        Qt.callLater(() => { nameField.forceActiveFocus(); nameField.selectAll(); });
    }
    Connections {
        target: root.controller
        function onAgentMutationSucceeded(session) {
            if (root.visible && root.submitting && session === root.session) root.closeRequested();
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
        radius: 12
        color: "#20212e"
        border.color: "#41445a"
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 14
            Text { text: "Rename contact"; font.pixelSize: 20; color: "#c0caf5" }
            TextField {
                id: nameField
                objectName: "renameAgentName"
                Layout.fillWidth: true
                placeholderText: "Name"
                enabled: !root.submitting
                onAccepted: root.submit()
                Keys.onEscapePressed: root.closeRequested()
            }
            Text {
                Layout.fillWidth: true
                // Reads as "Chat id  stays the same" with no chat to name, and
                // there is nothing reassuring about that.
                visible: root.session.length > 0
                text: "Chat id " + root.session + " stays the same, so history and pairings are kept."
                color: "#9ca1bd"
                elide: Text.ElideMiddle
                font.pixelSize: 12
            }
            Text {
                Layout.fillWidth: true
                visible: root.controller.errorMessage.length > 0 || !root.controller.connected
                text: root.controller.errorMessage || "Connect to the Host to rename a contact"
                color: "#e0af68"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Item { Layout.fillWidth: true }
                Button { text: "Cancel"; onClicked: root.closeRequested() }
                Button {
                    objectName: "renameAgentSubmit"
                    text: root.submitting ? "Renaming…" : "Rename · Enter"
                    enabled: !root.submitting && root.controller.connected
                        && nameField.text.trim().length > 0
                    onClicked: root.submit()
                }
            }
        }
    }
}
