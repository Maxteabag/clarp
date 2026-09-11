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
    property bool choosingModel: false
    property int catalogRevision: 0
    readonly property var providers: [
        {id:"claude",label:"Claude"}, {id:"codex",label:"Codex"},
        {id:"grok",label:"Grok"}, {id:"agy",label:"AGY"}, {id:"opencode",label:"OpenCode"}
    ]
    readonly property int selectedIndex: Math.max(0, providers.findIndex(p => p.id === backend))
    readonly property var modelChoices: {
        catalogRevision;
        const choices = Array.from(controller.modelsForBackend(backend));
        if (modelId && !choices.some(choice => choice.id === modelId)) choices.push({id:modelId,label:modelId});
        return choices;
    }
    signal closeRequested
    color: "#e61a1b26"
    Keys.onPressed: event => {
        if (event.key === Qt.Key_Escape) { back(); event.accepted = true; }
    }

    function focusSelected() {
        if (visible && !submitting) {
            if (poolEmpty) nameField.forceActiveFocus();
            else if (choosingModel) models.forceActiveFocus();
            else cards.itemAt(selectedIndex).forceActiveFocus();
        }
    }
    function selectProvider(index) {
        const next = providers[(index + providers.length) % providers.length].id;
        if (backend !== next) { backend = next; modelId = ""; effort = ""; }
    }
    function moveProvider(delta) { selectProvider(selectedIndex + delta); focusSelected(); }
    function showModels() { choosingModel = !choosingModel; Qt.callLater(focusSelected); }
    function back() {
        if (choosingModel) { choosingModel = false; Qt.callLater(focusSelected); } else cancel();
    }
    function cancel() { if (!submitting) { autoStart = false; closeRequested(); } }
    function open(wantedBackend, wantedModel, wantedEffort, anonymousMode) {
        anonymous = anonymousMode === 1 ? true : anonymousMode === 0 ? false : controller.anonymousAgents;
        const saved = controller.lastBackend || "codex";
        backend = wantedBackend || (providers.some(p => p.id === saved) ? saved : "codex");
        modelId = wantedModel;
        effort = wantedEffort;
        autoStart = wantedBackend.length > 0;
        submitting = false;
        poolEmpty = false;
        choosingModel = false;
        nameField.clear();
        controller.clearError();
        visible = true;
        Qt.callLater(maybeStart);
        Qt.callLater(focusSelected);
    }
    function maybeStart() {
        if (visible && autoStart && controller.connected) { autoStart = false; submit(); }
    }
    function submit() {
        if (submitting || !controller.connected || !backend) return;
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
    function cardKey(event, index) {
        if (event.isAutoRepeat && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) { event.accepted = true; return; }
        switch (event.key) {
        case Qt.Key_Left: case Qt.Key_Up: moveProvider(-1); break;
        case Qt.Key_Right: case Qt.Key_Down: moveProvider(1); break;
        case Qt.Key_Tab:
            if (event.modifiers & Qt.ShiftModifier) {
                if (index === 0) startButton.forceActiveFocus(); else moveProvider(-1);
            } else if (index === providers.length - 1) modelButton.forceActiveFocus(); else moveProvider(1);
            break;
        case Qt.Key_Backtab:
            if (index === 0) startButton.forceActiveFocus(); else moveProvider(-1);
            break;
        case Qt.Key_Return: case Qt.Key_Enter: submit(); break;
        case Qt.Key_M: showModels(); break;
        default: return;
        }
        event.accepted = true;
    }
    Connections {
        target: root.controller
        function onModelCatalogChanged() { root.catalogRevision++; }
        function onConnectedChanged() { root.maybeStart(); }
        function onLaunchPoolEmpty() {
            if (!root.visible || !root.submitting) return;
            root.submitting = false; root.poolEmpty = true; root.choosingModel = false;
            Qt.callLater(root.focusSelected);
        }
        function onAgentMutationSucceeded() { if (root.visible && root.submitting) root.closeRequested(); }
        function onErrorMessageChanged() {
            if (root.controller.errorMessage.length > 0) { root.submitting = false; Qt.callLater(root.focusSelected); }
        }
    }
    MouseArea { anchors.fill: parent }
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(680, parent.width - 32)
        height: form.implicitHeight + 40
        color: "#1a1b26"; border.color: "#41445a"
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 16
            TuiText { text: root.poolEmpty ? "New contact" : "New agent"; color: "#c0caf5"; font.pixelSize: 18 }
            RowLayout {
                Layout.fillWidth: true
                visible: !root.poolEmpty
                spacing: 8
                Repeater {
                    id: cards
                    model: root.providers
                    delegate: Button {
                        id: card
                        required property var modelData
                        required property int index
                        objectName: "providerCard-" + modelData.id
                        Layout.fillWidth: true
                        Layout.preferredWidth: 110
                        Layout.preferredHeight: 96
                        focusPolicy: Qt.StrongFocus
                        enabled: !root.submitting
                        Accessible.name: modelData.label
                        onActiveFocusChanged: if (activeFocus) root.selectProvider(index)
                        onClicked: { root.selectProvider(index); forceActiveFocus(); }
                        Keys.onShortcutOverride: event => {
                            if ([Qt.Key_Return,Qt.Key_Enter,Qt.Key_Escape].includes(event.key)) event.accepted = true;
                        }
                        Keys.onPressed: event => root.cardKey(event, index)
                        background: Rectangle {
                            color: root.selectedIndex === card.index ? "#303348" : card.hovered ? "#252738" : "#20212e"
                            border.color: root.selectedIndex === card.index ? "#bb9af7" : "#41445a"
                            border.width: card.activeFocus ? 2 : 1
                        }
                        contentItem: Column {
                            spacing: 10
                            topPadding: 12
                            Image {
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: 32; height: 32
                                source: "../../resources/backends/" + card.modelData.id + ".png"
                                fillMode: Image.PreserveAspectFit
                                sourceSize.width: 64; sourceSize.height: 64
                            }
                            TuiText {
                                width: parent.width; horizontalAlignment: Text.AlignHCenter
                                text: card.modelData.label; color: "#c0caf5"; font.pixelSize: 13
                            }
                        }
                    }
                }
            }
            ListView {
                id: models
                objectName: "launchModels"
                visible: root.choosingModel && !root.poolEmpty
                enabled: !root.submitting
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(180, count * 36)
                model: root.modelChoices
                currentIndex: Math.max(0, root.modelChoices.findIndex(choice => choice.id === root.modelId))
                clip: true
                keyNavigationEnabled: false
                function moveModel(delta) {
                    if (!count) return;
                    const next = (currentIndex + delta + count) % count;
                    root.modelId = root.modelChoices[next].id; root.effort = "";
                    positionViewAtIndex(next, ListView.Contain);
                }
                Keys.onShortcutOverride: event => {
                    if ([Qt.Key_Return,Qt.Key_Enter,Qt.Key_Escape].includes(event.key)) event.accepted = true;
                }
                Keys.onPressed: event => {
                    switch (event.key) {
                    case Qt.Key_Up: moveModel(-1); break;
                    case Qt.Key_Down: moveModel(1); break;
                    case Qt.Key_Return: case Qt.Key_Enter: root.submit(); break;
                    case Qt.Key_Tab: anonymousChoice.forceActiveFocus(); break;
                    default: return;
                    }
                    event.accepted = true;
                }
                delegate: TuiText {
                    required property var modelData
                    required property int index
                    width: models.width; height: 36; leftPadding: 10
                    verticalAlignment: Text.AlignVCenter
                    text: modelData.id ? modelData.label : "Default"
                    color: "#c0caf5"
                    Rectangle { anchors.fill: parent; z: -1; color: models.currentIndex === parent.index ? "#41445a" : "transparent" }
                    MouseArea { anchors.fill: parent; onClicked: { root.modelId = parent.modelData.id; root.submit(); } }
                }
            }
            TuiCheckBox {
                id: anonymousChoice
                visible: root.choosingModel && !root.poolEmpty
                text: "Anonymous"; checked: root.anonymous; enabled: !root.submitting
                onToggled: root.anonymous = checked
                KeyNavigation.tab: modelButton
                KeyNavigation.backtab: models
            }
            TuiTextField {
                id: nameField
                objectName: "launchContactName"
                visible: root.poolEmpty; enabled: !root.submitting
                Layout.fillWidth: true; placeholderText: "Name"
                onAccepted: root.submit()
                Keys.onEscapePressed: root.cancel()
            }
            TuiText {
                Layout.fillWidth: true
                visible: root.controller.errorMessage.length > 0 || !root.controller.connected
                text: root.controller.errorMessage || "Connecting…"
                color: "#e0af68"; wrapMode: Text.Wrap
            }
            RowLayout {
                TuiButton {
                    id: modelButton
                    visible: !root.poolEmpty
                    text: root.modelId ? root.modelId + " · M" : "Model · M"
                    enabled: !root.submitting
                    onClicked: root.showModels()
                    KeyNavigation.backtab: cards.itemAt(root.providers.length - 1)
                    KeyNavigation.tab: cancelButton
                }
                Item { Layout.fillWidth: true }
                TuiButton {
                    id: cancelButton
                    text: "Cancel"; enabled: !root.submitting
                    onClicked: root.cancel()
                    KeyNavigation.tab: startButton
                    KeyNavigation.backtab: modelButton
                }
                TuiButton {
                    id: startButton
                    text: root.submitting ? "Starting…" : root.poolEmpty ? "Create ↵" : "Start ↵"
                    enabled: !root.submitting && root.controller.connected && (!root.poolEmpty || nameField.text.trim().length > 0)
                    onClicked: root.submit()
                    KeyNavigation.tab: cards.itemAt(0)
                    KeyNavigation.backtab: cancelButton
                }
            }
        }
    }
}
