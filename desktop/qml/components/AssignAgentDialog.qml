pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    required property var controller
    property string session: ""
    property string mode: "auto"
    property bool submitting: false
    property bool returnToComposer: false
    signal closeRequested
    color: "#e61a1b26"
    function open(target, automatic, restoreComposer) {
        returnToComposer = restoreComposer === true;
        session = target;
        mode = "auto";
        submitting = false;
        nameField.clear();
        controller.clearError();
        visible = true;
        controller.loadAssignmentContacts(session);
        Qt.callLater(() => automaticOption.forceActiveFocus());
        if (automatic && controller.connected) submit();
    }
    function submit() {
        if (submitting || !controller.connected) return;
        const name = mode === "create" ? nameField.text.trim()
            : mode === "choose" ? String(contactField.currentValue || "") : "";
        if (mode !== "auto" && !name) return;
        submitting = true;
        controller.assignContact(session, mode, name);
    }
    Connections {
        target: root.controller
        function onContactAssignmentSucceeded(target: string) {
            if (root.visible && root.submitting && root.session === target) root.closeRequested();
        }
        function onErrorMessageChanged() {
            if (root.controller.errorMessage.length > 0) root.submitting = false;
        }
    }
    MouseArea { anchors.fill: parent }
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(580, parent.width - 32)
        height: form.implicitHeight + 40
        color: "#1a1b26"; border.color: "#41445a"
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 14
            TuiText { text: "Assign a contact"; color: "#c0caf5"; font.pixelSize: 18 }
            TuiText {
                Layout.fillWidth: true
                text: "Keep this agent’s backend, model, and conversation."
                color: "#9ca1bd"; wrapMode: Text.Wrap
            }
            TuiRadioButton {
                id: automaticOption
                objectName: "assignAutomatically"
                text: "Automatically assign · default"
                checked: root.mode === "auto"; enabled: !root.submitting
                onClicked: root.mode = "auto"
                Keys.onReturnPressed: root.submit()
                Keys.onEnterPressed: root.submit()
            }
            TuiRadioButton {
                text: "Choose a contact"
                checked: root.mode === "choose"; enabled: !root.submitting
                onClicked: root.mode = "choose"
            }
            ThemedComboBox {
                id: contactField
                objectName: "assignmentContact"
                visible: root.mode === "choose"
                enabled: !root.submitting
                Layout.fillWidth: true
                model: root.controller.assignmentContacts
                textRole: "name"; valueRole: "name"
            }
            TuiText {
                visible: root.mode === "choose" && root.controller.assignmentContacts.length === 0
                text: "No compatible contacts available. Create one below."
                color: "#9ca1bd"
                Layout.fillWidth: true; wrapMode: Text.Wrap
            }
            TuiRadioButton {
                text: "Create a new contact"
                checked: root.mode === "create"; enabled: !root.submitting
                onClicked: { root.mode = "create"; Qt.callLater(() => nameField.forceActiveFocus()); }
            }
            TuiTextField {
                id: nameField
                objectName: "assignmentNewName"
                visible: root.mode === "create"; enabled: !root.submitting
                Layout.fillWidth: true; placeholderText: "Contact name"
                onAccepted: root.submit()
            }
            TuiText {
                visible: root.controller.errorMessage.length > 0 || !root.controller.connected
                text: root.controller.errorMessage || "Connect to the Host to assign a contact."
                Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#e0af68"
            }
            RowLayout {
                Item { Layout.fillWidth: true }
                TuiButton { text: "Cancel"; enabled: !root.submitting; onClicked: root.closeRequested() }
                TuiButton {
                    text: root.submitting ? "Assigning…" : "Assign"
                    enabled: !root.submitting && root.controller.connected
                        && (root.mode === "auto" || (root.mode === "create" ? nameField.text.trim().length > 0 : contactField.currentIndex >= 0))
                    onClicked: root.submit()
                }
            }
        }
    }
}
