pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One-screen New Session hub, mirroring the iOS ModelFirstSessionHubView:
// provider marks, a model summary with an inline editor, contact cards with
// search and "Show all", a New contact row, and one confirm action.
// The Host does not expose a contact tier to the desktop, so there is no
// "recommended for this provider" filter here; every idle contact is listed.
Rectangle {
    id: root
    required property var controller
    signal closeRequested
    signal connectionRequested
    property bool restoreComposer: false
    property bool submitting: false
    property string backend: ""
    property string modelId: ""
    property string effort: ""
    property bool editingModel: false
    property bool showAll: false
    property bool newContact: false
    property bool choosingDirectory: false
    property string directory: "~"
    property string query: ""
    property string selectedKey: ""
    property int catalogRevision: 0
    readonly property var fallbackProviders: [
        {id: "claude", label: "Claude"}, {id: "codex", label: "Codex"}, {id: "grok", label: "Grok"},
        {id: "agy", label: "AGY"}, {id: "opencode", label: "OpenCode"}, {id: "deepseek", label: "DeepSeek"}
    ]
    readonly property var providers: {
        catalogRevision;
        const offered = Array.from(root.controller.backendOptions || []).map(option => ({
            id: String(option.id || ""), label: String(option.label || option.id || "")
        })).filter(option => option.id.length > 0);
        return offered.length > 0 ? offered : root.fallbackProviders;
    }
    readonly property int providerIndex: Math.max(0, providers.findIndex(provider => provider.id === root.backend))
    readonly property var modelChoices: { catalogRevision; return Array.from(root.controller.modelsForBackend(root.backend) || []); }
    readonly property var effortChoices: { catalogRevision; return Array.from(root.controller.effortsForModel(root.backend, root.modelId) || []); }
    readonly property string modelSummary: {
        const model = root.modelChoices.find(choice => String(choice.id) === root.modelId);
        const effortRow = root.effortChoices.find(choice => String(choice.id) === root.effort);
        const modelLabel = root.modelId.length === 0 ? "Server default" : String(model ? model.label : root.modelId);
        return root.effort.length === 0 ? modelLabel : modelLabel + " · " + String(effortRow ? effortRow.label : root.effort);
    }
    readonly property var rows: {
        root.controller.agentRevision;
        root.controller.contacts.count;
        const needle = root.query.trim().toLowerCase();
        const idle = Array.from(root.controller.matchingContacts(root.query)).map(contact => ({
            key: "contact:" + String(contact.name), kind: "contact", name: String(contact.name),
            session: "", symbol: String(contact.symbol || ""), subtitle: String(contact.description || ""), inChat: false
        }));
        idle.sort((a, b) => a.name.localeCompare(b.name));
        if (!root.showAll) return idle;
        const chats = Array.from(root.controller.matchingAgents(root.query)).map(agent => ({
            key: "agent:" + String(agent.session), kind: "agent", name: String(agent.name),
            session: String(agent.session), symbol: "", subtitle: String(agent.backend || ""), inChat: true
        })).filter(agent => needle.length === 0 || agent.name.toLowerCase().includes(needle));
        chats.sort((a, b) => a.name.localeCompare(b.name));
        return idle.concat(chats);
    }
    readonly property var selectedRow: root.rows.find(row => row.key === root.selectedKey) || null
    readonly property bool starting: root.submitting || String(root.controller.startingContact || "").length > 0
    readonly property bool canConfirm: root.controller.connected && !root.starting && !root.choosingDirectory
        && (root.newContact ? nameField.text.trim().length > 0 : root.selectedRow !== null)
    readonly property string confirmLabel: root.newContact ? "Create session"
        : root.selectedRow && root.selectedRow.inChat ? "Open chat" : "Create session"
    color: Theme.scrim
    objectName: "newSessionHub"

    function open(returnToComposer, contactMode) {
        restoreComposer = Boolean(returnToComposer);
        submitting = false; editingModel = false; showAll = false; newContact = false; choosingDirectory = false; pendingLaunch = "";
        query = ""; selectedKey = ""; modelId = ""; effort = "";
        nameField.text = "";
        directory = String(root.controller.lastWorkingDirectory || "~");
        const saved = String(root.controller.lastBackend || "");
        backend = providers.some(provider => provider.id === saved) ? saved : providers[0].id;
        root.controller.clearError();
        visible = true;
        Qt.callLater(() => { root.selectFirst(); search.forceActiveFocus(); });
    }
    // Esc steps back through the inline editors before it closes the hub.
    function stepBack() {
        if (choosingDirectory) { choosingDirectory = false; search.forceActiveFocus(); }
        else if (editingModel) editingModel = false;
        else if (newContact) { newContact = false; search.forceActiveFocus(); }
        else close();
    }
    // A command-line launch (`--backend claude --cwd …`) starts straight away
    // with the saved anonymous/contact preference, as the old launch page did.
    function openLaunch(wantedBackend, wantedModel, wantedEffort, anonymousMode, wantedDirectory) {
        open(false, false);
        if (wantedDirectory) { directory = String(wantedDirectory); root.controller.setLaunchDirectory(directory); }
        if (wantedBackend && providers.some(provider => provider.id === String(wantedBackend))) backend = String(wantedBackend);
        modelId = String(wantedModel || ""); effort = String(wantedEffort || "");
        if (!wantedBackend) return;
        const anonymous = anonymousMode === 1 ? true : anonymousMode === 0 ? false : Boolean(root.controller.anonymousAgents);
        if (!root.controller.connected) { pendingLaunch = anonymous ? "anonymous" : "contact"; return; }
        submitting = anonymous ? root.controller.startAnonymousAgent(backend, modelId, effort)
            : root.controller.startAvailableContact(backend, modelId, effort);
    }
    property string pendingLaunch: ""
    function close() {
        visible = false;
        if (restoreComposer) Qt.callLater(() => root.controller.requestComposerFocus(root.controller.panes.activePaneId));
        closeRequested();
    }
    function selectFirst() { root.selectedKey = root.rows.length > 0 ? root.rows[0].key : ""; }
    function moveSelection(delta) {
        if (root.rows.length === 0) { root.selectedKey = ""; return; }
        const index = root.rows.findIndex(row => row.key === root.selectedKey);
        const next = index < 0 ? 0 : Math.max(0, Math.min(root.rows.length - 1, index + delta));
        root.selectedKey = root.rows[next].key;
        cards.positionViewAtIndex(next, ListView.Contain);
    }
    function selectProvider(index) {
        const next = root.providers[(index + root.providers.length) % root.providers.length].id;
        if (root.backend !== next) { root.backend = next; root.modelId = ""; root.effort = ""; }
    }
    function confirm() {
        if (!root.canConfirm) return;
        root.controller.clearError();
        if (root.newContact) {
            root.submitting = true;
            root.controller.createAgent(nameField.text.trim(), root.directory, root.backend, root.modelId, root.effort, "", "fresh", "", []);
            return;
        }
        const row = root.selectedRow;
        if (row.inChat) { root.controller.selectSession(row.session); root.close(); return; }
        root.controller.setLaunchDirectory(root.directory);
        root.submitting = root.controller.quickStartContact(row.name, root.backend, root.modelId, root.effort);
    }
    function handleKey(event) {
        if (event.key === Qt.Key_Escape) {
            root.stepBack();
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (!event.isAutoRepeat) root.confirm();
        } else if (event.key === Qt.Key_Down) root.moveSelection(1);
        else if (event.key === Qt.Key_Up) root.moveSelection(-1);
        else if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_Left) root.selectProvider(root.providerIndex - 1);
        else if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_Right) root.selectProvider(root.providerIndex + 1);
        else return;
        event.accepted = true;
    }
    onRowsChanged: if (visible && root.selectedRow === null) root.selectFirst()

    Connections {
        target: root.controller
        function onModelCatalogChanged() { root.catalogRevision++; }
        function onConnectedChanged() {
            if (!root.visible || !root.controller.connected || root.pendingLaunch.length === 0) return;
            const anonymous = root.pendingLaunch === "anonymous"; root.pendingLaunch = "";
            root.submitting = anonymous ? root.controller.startAnonymousAgent(root.backend, root.modelId, root.effort)
                : root.controller.startAvailableContact(root.backend, root.modelId, root.effort);
        }
        function onLaunchPoolEmpty() {
            if (!root.visible || !root.submitting) return;
            root.submitting = false; root.newContact = true;
            Qt.callLater(() => nameField.forceActiveFocus());
        }
        function onAgentMutationSucceeded() { if (root.visible && root.submitting) root.close(); }
        function onContactLaunchChanged() {
            if (root.visible && root.submitting && String(root.controller.startingContact || "").length === 0
                && String(root.controller.errorMessage || "").length === 0 && root.selectedRow && !root.newContact)
                root.close();
        }
        function onErrorMessageChanged() { if (String(root.controller.errorMessage || "").length > 0) root.submitting = false; }
    }

    MouseArea { anchors.fill: parent; onClicked: root.close() }

    Rectangle {
        id: card
        width: Math.min(760, parent.width - 32)
        height: Math.min(660, parent.height - 48)
        anchors.centerIn: parent
        color: Theme.window
        border.color: Theme.border
        radius: 0
        MouseArea { anchors.fill: parent; onClicked: mouse => mouse.accepted = true }
        Keys.onPressed: event => root.handleKey(event)

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                TuiText { text: "New Session"; color: Theme.text; font.pixelSize: 18; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                TuiButton { text: "Cancel · Esc"; onClicked: root.close() }
                TuiButton {
                    id: confirmButton
                    objectName: "newSessionConfirm"
                    text: root.starting ? "Starting…" : root.confirmLabel + " · Enter"
                    enabled: root.canConfirm
                    onClicked: root.confirm()
                }
            }

            RowLayout {
                visible: !root.controller.connected
                Layout.fillWidth: true
                spacing: 12
                TuiText { Layout.fillWidth: true; text: "Connect to a server first"; color: Theme.warning; wrapMode: Text.Wrap }
                TuiButton { text: "Host connection…"; onClicked: { root.close(); root.connectionRequested(); } }
            }
            Item { visible: !root.controller.connected; Layout.fillHeight: true }

            // Provider marks.
            ColumnLayout {
                visible: root.controller.connected
                Layout.fillWidth: true
                spacing: 6
                TuiText { text: "PROVIDER"; color: Theme.muted; font.pixelSize: 11; font.letterSpacing: 0.8 }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Repeater {
                        id: chips
                        model: root.providers
                        delegate: Rectangle {
                            id: chip
                            required property var modelData
                            required property int index
                            readonly property bool selected: root.backend === String(modelData.id)
                            objectName: "providerChip-" + String(modelData.id)
                            width: chipRow.implicitWidth + 22
                            height: 40
                            color: selected ? Theme.raised : chipHover.hovered ? Theme.hover : "transparent"
                            border.width: selected ? 2 : 1
                            border.color: selected ? Theme.accent : Theme.border
                            activeFocusOnTab: true
                            Keys.onPressed: event => {
                                if (event.key === Qt.Key_Left) { root.selectProvider(chip.index - 1); event.accepted = true; }
                                else if (event.key === Qt.Key_Right) { root.selectProvider(chip.index + 1); event.accepted = true; }
                                else root.handleKey(event);
                            }
                            onActiveFocusChanged: if (activeFocus && !selected) root.selectProvider(index)
                            HoverHandler { id: chipHover }
                            TapHandler { onTapped: { root.selectProvider(chip.index); chip.forceActiveFocus(); } }
                            RowLayout {
                                id: chipRow
                                anchors.centerIn: parent
                                spacing: 8
                                Rectangle {
                                    Layout.preferredWidth: 24; Layout.preferredHeight: 24; radius: 12
                                    color: chip.selected ? Theme.accent : Theme.control
                                    TuiText {
                                        anchors.centerIn: parent
                                        text: String(chip.modelData.label).slice(0, 1).toUpperCase()
                                        color: chip.selected ? Theme.accentText : Theme.text
                                        font.pixelSize: 12; font.weight: Font.Bold
                                    }
                                }
                                TuiText { text: String(chip.modelData.label); color: chip.selected ? Theme.text : Theme.secondary; font.pixelSize: 13 }
                            }
                        }
                    }
                }
            }

            // Model summary and inline editor.
            ColumnLayout {
                visible: root.controller.connected
                Layout.fillWidth: true
                spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    TuiText { text: "MODEL"; color: Theme.muted; font.pixelSize: 11; font.letterSpacing: 0.8 }
                    TuiText { objectName: "modelSummary"; Layout.fillWidth: true; text: root.modelSummary; color: Theme.text; elide: Text.ElideRight }
                    TuiButton { text: root.editingModel ? "Done" : "Edit"; implicitHeight: 26; onClicked: root.editingModel = !root.editingModel }
                }
                RowLayout {
                    visible: root.editingModel
                    Layout.fillWidth: true
                    spacing: 10
                    ThemedComboBox {
                        id: modelField
                        Layout.fillWidth: true
                        model: [{id: "", label: "Server default"}].concat(root.modelChoices)
                        textRole: "label"; valueRole: "id"
                        currentIndex: Math.max(0, model.findIndex(choice => String(choice.id) === root.modelId))
                        onActivated: { root.modelId = String(currentValue || ""); root.effort = ""; }
                    }
                    ThemedComboBox {
                        id: effortField
                        Layout.preferredWidth: 200
                        model: [{id: "", label: "Server default"}].concat(root.effortChoices)
                        textRole: "label"; valueRole: "id"
                        currentIndex: Math.max(0, model.findIndex(choice => String(choice.id) === root.effort))
                        onActivated: root.effort = String(currentValue || "")
                    }
                }
            }

            // Directory: a compact row, never a blocking first step.
            RowLayout {
                visible: root.controller.connected && !root.choosingDirectory
                Layout.fillWidth: true
                spacing: 10
                TuiText { text: "DIRECTORY"; color: Theme.muted; font.pixelSize: 11; font.letterSpacing: 0.8 }
                TuiText { objectName: "directoryLabel"; Layout.fillWidth: true; text: root.directory; color: Theme.secondary; elide: Text.ElideMiddle }
                TuiButton { text: "Change…"; implicitHeight: 26; onClicked: { root.choosingDirectory = true; directoryPicker.open(); Qt.callLater(directoryPicker.focusSearch); } }
            }
            LaunchDirectoryPicker {
                id: directoryPicker
                visible: root.choosingDirectory
                Layout.fillWidth: true
                Layout.fillHeight: true
                controller: root.controller
                onChosen: (path, label) => { root.directory = path; root.controller.setLaunchDirectory(path); root.choosingDirectory = false; search.forceActiveFocus(); }
                onCancelRequested: { root.choosingDirectory = false; search.forceActiveFocus(); }
            }

            // Contacts.
            ColumnLayout {
                visible: root.controller.connected && !root.choosingDirectory
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    TuiText { text: "CONTACT"; color: Theme.muted; font.pixelSize: 11; font.letterSpacing: 0.8 }
                    Item { Layout.fillWidth: true }
                    TuiCheckBox { id: showAllBox; objectName: "showAllContacts"; text: "Show all"; checked: root.showAll; onToggled: root.showAll = checked }
                    TuiButton { objectName: "newContactButton"; text: root.newContact ? "Choose a contact" : "New contact"; implicitHeight: 26
                        onClicked: { root.newContact = !root.newContact; Qt.callLater(() => (root.newContact ? nameField : search).forceActiveFocus()); } }
                }
                TuiTextField {
                    id: nameField
                    objectName: "newContactName"
                    visible: root.newContact
                    Layout.fillWidth: true
                    placeholderText: "Name for the new contact"
                    Keys.onPressed: event => root.handleKey(event)
                }
                TuiTextField {
                    id: search
                    objectName: "contactSearch"
                    visible: !root.newContact
                    Layout.fillWidth: true
                    placeholderText: "Search contacts"
                    text: root.query
                    onTextChanged: { root.query = text; Qt.callLater(root.selectFirst); }
                    Keys.onPressed: event => root.handleKey(event)
                }
                ListView {
                    id: cards
                    objectName: "contactCards"
                    visible: !root.newContact
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 4
                    model: root.rows
                    delegate: Rectangle {
                        id: cardRow
                        required property var modelData
                        required property int index
                        readonly property bool selected: root.selectedKey === String(modelData.key)
                        objectName: "contactCard-" + String(modelData.name)
                        width: ListView.view.width
                        height: 56
                        color: selected ? Theme.raised : rowHover.hovered ? Theme.hover : "transparent"
                        border.width: 1
                        border.color: selected ? Theme.accent : Theme.rule
                        HoverHandler { id: rowHover }
                        TapHandler {
                            onTapped: root.selectedKey = String(cardRow.modelData.key)
                            onDoubleTapped: { root.selectedKey = String(cardRow.modelData.key); root.confirm(); }
                        }
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10; anchors.rightMargin: 12
                            spacing: 12
                            AgentAvatar {
                                Layout.preferredWidth: 36; Layout.preferredHeight: 36
                                controller: root.controller
                                session: String(cardRow.modelData.session || "")
                                name: String(cardRow.modelData.name || "")
                                // The Host's avatar_symbol is an iOS SF Symbol name, not a glyph;
                                // fall back to the initial and prefer the persona portrait.
                                symbol: ""
                                avatarSize: 36; cornerRadius: 18
                                showPortrait: true
                                portraitSource: {
                                    root.controller.avatarRevision;
                                    return !cardRow.modelData.inChat && root.controller.contactAvatarSource
                                        ? root.controller.contactAvatarSource(String(cardRow.modelData.name)) : "";
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                TuiText { text: String(cardRow.modelData.name); color: Theme.text; font.pixelSize: 14; font.weight: Font.Medium; elide: Text.ElideRight; Layout.fillWidth: true }
                                TuiText { visible: text.length > 0; text: String(cardRow.modelData.subtitle || ""); color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                            }
                            Rectangle {
                                visible: cardRow.modelData.inChat
                                Layout.preferredWidth: inChatText.implicitWidth + 14; Layout.preferredHeight: 20
                                color: Theme.control; border.color: Theme.border
                                TuiText { id: inChatText; anchors.centerIn: parent; text: "In chat"; color: Theme.secondary; font.pixelSize: 11 }
                            }
                            Rectangle {
                                visible: cardRow.selected
                                Layout.preferredWidth: 22; Layout.preferredHeight: 22; radius: 11
                                color: Theme.accent
                                TuiText { anchors.centerIn: parent; text: "✓"; color: Theme.accentText; font.pixelSize: 13; font.weight: Font.Bold }
                            }
                        }
                    }
                    TuiText {
                        anchors.centerIn: parent
                        visible: cards.count === 0
                        text: root.query.length > 0 ? "No contact matches" : root.showAll ? "No contacts" : "Every contact is in a chat · Show all to open one"
                        color: Theme.muted
                        font.pixelSize: 12
                    }
                }
            }

            TuiText {
                visible: String(root.controller.errorMessage || "").length > 0
                Layout.fillWidth: true
                text: String(root.controller.errorMessage || "")
                color: Theme.warning
                wrapMode: Text.Wrap
            }
        }
    }
}
