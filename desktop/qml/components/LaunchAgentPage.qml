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
    property bool choosingSessions: false
    readonly property bool historyLoading: controller.pastSessionsLoading || false
    property bool continueLatest: false
    property string resumeId: ""
    property bool choosingModel: false
    property bool choosingDirectory: true
    property string directory: "~"
    property string directoryLabel: "~"
    property int catalogRevision: 0
    readonly property bool canResume: catalogRevision === 0 || controller.backendSupportsResume(backend)
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
    color: "#1a1b26"
    Keys.onPressed: event => {
        if (event.key === Qt.Key_Escape) { back(); event.accepted = true; }
    }

    function focusSelected() {
        if (visible && !submitting) {
            if (choosingDirectory) directoryPicker.focusSearch();
            else if (poolEmpty) nameField.forceActiveFocus();
            else if (choosingSessions) sessions.forceActiveFocus();
            else if (choosingModel) models.forceActiveFocus();
            else cards.itemAt(selectedIndex).forceActiveFocus();
        }
    }
    function selectProvider(index) {
        const next = providers[(index + providers.length) % providers.length].id;
        if (backend !== next) { backend = next; modelId = ""; effort = ""; }
    }
    function moveProvider(delta) { selectProvider(selectedIndex + delta); focusSelected(); }
    function showSessions(latest) {
        if (submitting || !controller.connected || !canResume) return;
        controller.clearError();
        choosingModel = false; choosingSessions = true; continueLatest = latest; resumeId = "";
        controller.loadPastSessions(directory, backend);
        Qt.callLater(focusSelected);
    }
    function resumeSelected() {
        if (submitting || controller.pastSessionsLoading) return;
        const rows = controller.pastSessions;
        if (!rows.length) return;
        resumeId = String(rows[Math.max(0, sessions.currentIndex)].id);
        submitting = controller.resumeLaunchSession(backend, resumeId, anonymous);
    }
    function showModels() { choosingModel = !choosingModel; Qt.callLater(focusSelected); }
    function back() {
        if (choosingSessions) { choosingSessions = false; continueLatest = false; Qt.callLater(focusSelected); }
        else if (choosingModel) { choosingModel = false; Qt.callLater(focusSelected); }
        else if (choosingDirectory) directoryPicker.back();
        else { choosingDirectory = true; Qt.callLater(focusSelected); }
    }
    function cancel() { if (!submitting) { autoStart = false; closeRequested(); } }
    function open(wantedBackend, wantedModel, wantedEffort, anonymousMode, wantedDirectory) {
        directory = wantedDirectory || "~"; directoryLabel = directory;
        choosingDirectory = !wantedDirectory;
        controller.setLaunchDirectory(directory);
        anonymous = anonymousMode === 1 ? true : anonymousMode === 0 ? false : controller.anonymousAgents;
        const saved = controller.lastBackend || "codex";
        backend = wantedBackend || (providers.some(p => p.id === saved) ? saved : "codex");
        modelId = wantedModel;
        effort = wantedEffort;
        autoStart = wantedBackend.length > 0;
        submitting = false;
        poolEmpty = false;
        choosingModel = false; choosingSessions = false; continueLatest = false; resumeId = "";
        nameField.clear();
        controller.clearError();
        visible = true;
        if (choosingDirectory) directoryPicker.open();
        Qt.callLater(maybeStart);
        Qt.callLater(focusSelected);
    }
    function maybeStart() {
        if (visible && !choosingDirectory && autoStart && controller.connected) { autoStart = false; submit(); }
    }
    function submit() {
        if (choosingDirectory) { directoryPicker.confirm(); return; }
        if (choosingSessions && !poolEmpty) { resumeSelected(); return; }
        if (submitting || !controller.connected || !backend) return;
        controller.clearError();
        if (poolEmpty) {
            if (!nameField.text.trim()) return;
            submitting = true;
            controller.createAgent(nameField.text.trim(), directory,
                backend, resumeId ? "" : modelId, resumeId ? "" : effort, "", resumeId ? "resume" : "fresh", resumeId, []);
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
        case Qt.Key_C: showSessions(true); break;
        case Qt.Key_R: showSessions(false); break;
        default: return;
        }
        event.accepted = true;
    }
    Connections {
        target: root.controller
        function onPastSessionsChanged() {
            if (!root.visible || !root.choosingSessions || root.controller.pastSessionsLoading) return;
            sessions.currentIndex = 0;
            if (root.continueLatest) { root.continueLatest = false; root.resumeSelected(); }
            Qt.callLater(root.focusSelected);
        }
        function onModelCatalogChanged() { root.catalogRevision++; }
        function onConnectedChanged() { root.maybeStart(); }
        function onLaunchPoolEmpty() {
            if (!root.visible || !root.submitting) return;
            root.submitting = false; root.poolEmpty = true; root.choosingModel = false;
            Qt.callLater(root.focusSelected);
        }
        function onAgentMutationSucceeded() { if (root.visible && root.submitting) root.closeRequested(); }
        function onErrorMessageChanged() {
            if (root.controller.errorMessage.length > 0) { root.submitting = false; root.continueLatest = false; Qt.callLater(root.focusSelected); }
        }
    }
    MouseArea { anchors.fill: parent }
    Item {
        anchors.centerIn: parent
        width: Math.min(680, parent.width - 32)
        height: form.implicitHeight + 40
        ColumnLayout {
            id: form
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
            spacing: 16
            TuiText { text: root.choosingDirectory ? "Directory" : root.poolEmpty ? "New contact" : root.choosingSessions ? "Resume session" : "New agent"; color: "#c0caf5"; font.pixelSize: 18 }
            LaunchDirectoryPicker {
                id: directoryPicker
                Layout.fillWidth: true
                controller: root.controller
                visible: root.choosingDirectory
                onChosen: (path, label) => {
                    root.directory = path; root.directoryLabel = label;
                    root.controller.setLaunchDirectory(path);
                    root.choosingDirectory = false;
                    Qt.callLater(root.maybeStart); Qt.callLater(root.focusSelected);
                }
                onCancelRequested: root.cancel()
            }
            TuiText {
                visible: !root.choosingDirectory
                Layout.fillWidth: true
                text: root.directoryLabel; color: "#9ca1bd"; elide: Text.ElideMiddle
            }
            RowLayout {
                Layout.fillWidth: true
                visible: !root.choosingDirectory && !root.poolEmpty && !root.choosingSessions
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
                id: sessions
                objectName: "launchSessions"
                visible: root.choosingSessions && !root.choosingDirectory && !root.poolEmpty
                enabled: !root.submitting
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(280, Math.max(1, count) * 52)
                clip: true
                model: root.controller.pastSessions || []
                currentIndex: 0
                keyNavigationEnabled: false
                Keys.onShortcutOverride: event => {
                    if ([Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape].includes(event.key)) event.accepted = true;
                }
                Keys.onPressed: event => {
                    if ([Qt.Key_Down, Qt.Key_Up, Qt.Key_Tab, Qt.Key_Backtab].includes(event.key)) {
                        const delta = event.key === Qt.Key_Up || event.key === Qt.Key_Backtab || (event.modifiers & Qt.ShiftModifier) ? -1 : 1;
                        if (count) currentIndex = (currentIndex + delta + count) % count;
                        positionViewAtIndex(currentIndex, ListView.Contain);
                    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.resumeSelected();
                    else return;
                    event.accepted = true;
                }
                TuiText {
                    visible: sessions.count === 0
                    text: root.controller.pastSessionsLoading ? "Loading…" : "No previous sessions"
                    color: "#9ca1bd"
                }
                delegate: Rectangle {
                    id: sessionRow
                    required property var modelData
                    required property int index
                    width: sessions.width; height: 52
                    color: sessions.currentIndex === index ? "#303348" : "transparent"
                    border.color: sessions.currentIndex === index ? "#bb9af7" : "transparent"
                    Column {
                        anchors { fill: parent; margins: 7 }
                        TuiText { width: parent.width; text: String(sessionRow.modelData.title || sessionRow.modelData.preview || sessionRow.modelData.id); elide: Text.ElideRight; color: "#c0caf5" }
                        TuiText { text: new Date(Number(sessionRow.modelData.mtime) * 1000).toLocaleString(); color: "#9ca1bd"; font.pixelSize: 11 }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { sessions.currentIndex = parent.index; root.resumeSelected(); } }
                }
            }
            ListView {
                id: models
                objectName: "launchModels"
                visible: !root.choosingDirectory && root.choosingModel && !root.poolEmpty
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
                visible: !root.choosingDirectory && root.choosingModel && !root.poolEmpty
                text: "Anonymous"; checked: root.anonymous; enabled: !root.submitting
                onToggled: root.anonymous = checked
                KeyNavigation.tab: modelButton
                KeyNavigation.backtab: models
            }
            TuiTextField {
                id: nameField
                objectName: "launchContactName"
                visible: !root.choosingDirectory && root.poolEmpty; enabled: !root.submitting
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
                    visible: !root.choosingDirectory && !root.poolEmpty && !root.choosingSessions
                    text: root.modelId ? root.modelId + " · M" : "Model · M"
                    enabled: !root.submitting
                    onClicked: root.showModels()
                    KeyNavigation.backtab: cards.itemAt(root.providers.length - 1)
                    KeyNavigation.tab: cancelButton
                }
                TuiButton {
                    visible: !root.choosingDirectory && !root.poolEmpty && !root.choosingSessions
                    text: "Continue · C"; enabled: !root.submitting && root.canResume
                    onClicked: root.showSessions(true)
                }
                TuiButton {
                    visible: !root.choosingDirectory && !root.poolEmpty && !root.choosingSessions
                    text: "Resume · R"; enabled: !root.submitting && root.canResume
                    onClicked: root.showSessions(false)
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
                    text: root.submitting ? "Starting…" : root.choosingDirectory ? "Continue ↵" : root.poolEmpty ? "Create ↵" : root.choosingSessions ? "Resume ↵" : "Start ↵"
                    enabled: !root.submitting && (root.choosingDirectory || root.controller.connected) && (!root.poolEmpty || nameField.text.trim().length > 0) && (!root.choosingSessions || root.poolEmpty || (!root.controller.pastSessionsLoading && sessions.count > 0))
                    onClicked: root.submit()
                    KeyNavigation.tab: cards.itemAt(0)
                    KeyNavigation.backtab: cancelButton
                }
            }
        }
    }
}
