pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property var controller
    property string backend: ""
    property string modelId: ""
    property string effort: ""
    property bool anonymous: true
    property bool autoStart: false
    property bool submitting: false
    property bool poolEmpty: false
    property int catalogRevision: 0
    signal closeRequested
    color: "#e61a1b26"

    function open(wantedBackend, wantedModel, wantedEffort, anonymousMode) {
        anonymous = anonymousMode === 1 ? true : anonymousMode === 0 ? false : controller.anonymousAgents;
        backend = wantedBackend;
        modelId = wantedModel;
        effort = wantedEffort;
        autoStart = backend.length > 0;
        submitting = false;
        poolEmpty = false;
        nameField.clear();
        controller.clearError();
        visible = true;
        Qt.callLater(maybeStart);
        Qt.callLater(() => backendField.forceActiveFocus());
    }
    function maybeStart() {
        if (visible && autoStart && controller.connected) {
            autoStart = false;
            submit();
        }
    }
    function submit() {
        if (submitting || !controller.connected || backend.length === 0) return;
        controller.clearError();
        if (poolEmpty) {
            if (!nameField.text.trim()) return;
            submitting = true;
            controller.createAgent(nameField.text.trim(), controller.lastWorkingDirectory || "~",
                backend, modelId, effort, "", "fresh", "", []);
        } else {
            submitting = anonymous ? controller.startAnonymousAgent(backend, modelId, effort)
                : controller.startAvailableContact(backend, modelId, effort);
        }
    }
    Connections {
        target: root.controller
        function onModelCatalogChanged() { root.catalogRevision++; }
        function onConnectedChanged() { root.maybeStart(); }
        function onLaunchPoolEmpty() {
            if (!root.visible || !root.submitting) return;
            root.submitting = false;
            root.poolEmpty = true;
            Qt.callLater(() => nameField.forceActiveFocus());
        }
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
        width: Math.min(560, parent.width - 32)
        height: form.implicitHeight + 40
        color: "#1a1b26"
        border.color: "#41445a"
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 14
            TuiText { text: "Start an agent"; color: "#c0caf5"; font.pixelSize: 18 }
            TuiText { text: "Backend"; color: "#9ca1bd" }
            ThemedComboBox {
                id: backendField
                objectName: "launchBackend"
                Layout.fillWidth: true
                enabled: !root.submitting
                model: [ {id: "", label: "Choose a backend…"},
                    {id: "claude", label: "Claude"}, {id: "codex", label: "Codex"},
                    {id: "grok", label: "Grok"}, {id: "agy", label: "AGY"},
                    {id: "opencode", label: "OpenCode"} ]
                textRole: "label"; valueRole: "id"
                currentIndex: Math.max(0, indexOfValue(root.backend))
                onActivated: {
                    if (root.backend.length > 0) { root.modelId = ""; root.effort = ""; }
                    root.backend = String(currentValue);
                }
            }
            TuiCheckBox {
                text: "Start anonymously"
                checked: root.anonymous
                enabled: !root.submitting && !root.poolEmpty
                onToggled: root.anonymous = checked
            }
            TuiText { text: "Model"; color: "#9ca1bd" }
            ThemedComboBox {
                Layout.fillWidth: true
                enabled: !root.submitting
                model: {
                    root.catalogRevision;
                    const choices = Array.from(root.controller.modelsForBackend(root.backend));
                    if (root.modelId && !choices.some(choice => choice.id === root.modelId))
                        choices.push({id: root.modelId, label: root.modelId});
                    return choices;
                }
                textRole: "label"; valueRole: "id"
                currentIndex: Math.max(0, indexOfValue(root.modelId))
                onActivated: { root.modelId = String(currentValue); root.effort = ""; }
            }
            TuiText {
                visible: root.modelId.length > 0
                text: "Model: " + root.modelId + (root.effort ? " · " + root.effort : "")
                color: "#9ca1bd"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
            }
            TuiText {
                Layout.fillWidth: true
                text: root.anonymous ? "Start anonymously. Ctrl+A assigns a contact later."
                    : root.poolEmpty ? "No contacts are available. Create a new contact, or cancel."
                    : "An available contact will be picked automatically."
                color: "#9ca1bd"; wrapMode: Text.Wrap
            }
            TuiTextField {
                id: nameField
                objectName: "launchContactName"
                visible: root.poolEmpty
                enabled: !root.submitting
                Layout.fillWidth: true
                placeholderText: "New contact name"
                onAccepted: root.submit()
            }
            TuiText {
                Layout.fillWidth: true
                visible: root.controller.errorMessage.length > 0 || !root.controller.connected
                text: root.controller.errorMessage || "Waiting for the Host…"
                color: "#e0af68"; wrapMode: Text.Wrap
            }
            RowLayout {
                Item { Layout.fillWidth: true }
                TuiButton {
                    text: "Cancel"; enabled: !root.submitting
                    onClicked: { root.autoStart = false; root.closeRequested(); }
                }
                TuiButton {
                    text: root.submitting ? "Starting…" : root.poolEmpty ? "Create & start" : "Start"
                    enabled: !root.submitting && root.controller.connected && root.backend.length > 0
                        && (!root.poolEmpty || nameField.text.trim().length > 0)
                    onClicked: root.submit()
                }
            }
        }
    }
}
