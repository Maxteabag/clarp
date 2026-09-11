pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCore as Core
import "components"

ApplicationWindow {
    id: root

    property string relaunchSession: ""
    property string relaunchName: ""
    property string voiceSession: ""
    property string voiceName: ""
    property string selectedSurface: "chats"
    property real uiScale: 1.15
    property bool sidebarVisible: false
    property bool shortcutsVisible: true
    property real sidebarExpandedWidth: 354
    property bool redesignedSidebarSized: false
    readonly property bool settingsOverlayVisible: root.selectedSurface === "settings" && root.overlayVisible()

    width: 1360
    height: 900
    minimumWidth: Math.max(760, sidebarVisible ? Math.ceil(624 * uiScale) : 760)
    minimumHeight: 520
    visible: true
    title: app.selectedName.length > 0 ? app.selectedName + " — Clarp" : "Clarp"
    color: "#1a1b26"
    // Basic supplies control-specific defaults that can override the system
    // palette. Explicit window roles also propagate into popups and menus.
    palette.window: "#1a1b26"
    palette.base: "#1a1b26"
    palette.alternateBase: "#20212e"
    palette.button: "#292b3a"
    palette.toolTipBase: "#292b3a"
    palette.windowText: "#c0caf5"
    palette.text: "#c0caf5"
    palette.buttonText: "#c0caf5"
    palette.toolTipText: "#c0caf5"
    palette.brightText: "#1a1b26"
    palette.placeholderText: "#8d93b0"
    palette.highlight: "#bb9af7"
    palette.accent: "#bb9af7"
    palette.highlightedText: "#1a1b26"
    palette.link: "#7aa2f7"
    palette.linkVisited: "#bb9af7"
    palette.light: "#565b76"
    palette.midlight: "#41445a"
    palette.mid: "#41445a"
    palette.dark: "#bb9af7"
    palette.shadow: "#14151d"
    palette.disabled.text: "#8d93b0"
    palette.disabled.windowText: "#8d93b0"
    palette.disabled.buttonText: "#8d93b0"

    function openLaunchAgent(backend, model, effort, anonymousMode) {
        launchAgent.open(backend, model, effort, anonymousMode);
    }

    function composerOwnsFocus() {
        return root.activeFocusItem && root.activeFocusItem.objectName === "paneComposerEditor";
    }

    function overlayVisible() {
        return assignAgent.visible || launchAgent.visible || previewVersionPanel.visible || quickNewAgent.visible || renameAgent.visible || quickSwitcher.visible || voiceDialog.visible || orchestrator.visible
            || startAgent.visible || overview.visible || connection.visible
            || queueDialog.visible || profilePanel.visible || reportView.visible
            || settingsPanel.dialogOpen;
    }

    function workspaceAvailable() {
        return root.selectedSurface === "chats" && !root.overlayVisible();
    }

    function focusConversation() {
        app.requestComposerFocus("");
        Qt.callLater(() => {
            if (root.workspaceAvailable()) workspace.focusNavigation();
        });
    }

    function movePane(direction) {
        app.panes.navigate(direction);
        app.requestComposerFocus(app.panes.activePaneId);
    }

    function setUiScale(value) {
        root.uiScale = Math.max(1.0, Math.min(1.4, Math.round(value * 20) / 20));
        Qt.callLater(root.restoreSurfaceFocus);
    }

    function restoreSurfaceFocus() {
        if (root.overlayVisible()) return;
        if (root.selectedSurface === "settings") settingsPanel.focusCurrent();
        else if (root.selectedSurface === "chats") app.requestComposerFocus(app.panes.activePaneId);
    }

    onSelectedSurfaceChanged: Qt.callLater(root.restoreSurfaceFocus)
    onSettingsOverlayVisibleChanged: {
        if (!settingsOverlayVisible && selectedSurface === "settings")
            Qt.callLater(root.restoreSurfaceFocus);
    }

    function runCommand(action) {
        if (action === "shortcut-bar") { root.shortcutsVisible = !root.shortcutsVisible; return; }
        let layoutChanged = false;
        if (action === "escape") {
            if (keyboard.contextName === "search") rail.focusCurrentAgent();
            else if (keyboard.contextName === "sidebar") root.focusConversation();
            else root.escapeFocus();
        } else if (action === "next-attention") {
            const session = app.nextAttentionSession();
            if (session.length > 0) {
                root.selectedSurface = "chats";
                app.selectSession(session);
                app.requestComposerFocus(app.panes.activePaneId);
            }
        } else if (action === "focus-sidebar") {
            root.sidebarVisible = true;
            app.requestComposerFocus("");
            Qt.callLater(rail.focusCurrentAgent);
        } else if (action === "focus-pane") {
            root.focusConversation();
        } else if (action === "focus-composer") {
            app.requestComposerFocus(app.panes.activePaneId);
        } else if (action === "toggle-focus") {
            root.runCommand(rail.ownsFocus(root.activeFocusItem) ? "focus-pane" : "focus-sidebar");
        } else if (action === "agent-next" || action === "agent-previous") {
            rail.moveSelection(action === "agent-next" ? 1 : -1);
        } else if (action === "agent-open") {
            rail.openSelection();
        } else if (action === "agent-search") {
            rail.focusSearch();
        } else if (action === "switcher") {
            quickSwitcher.open(root.composerOwnsFocus());
        } else if (action === "preview-versions") {
            previewVersionPanel.visible = true;
        } else if (action === "assign-agent" || action === "auto-assign-agent") {
            if (app.selectedSession.length > 0 && !app.isPairSession(app.selectedSession))
                assignAgent.open(app.selectedSession, action === "auto-assign-agent", root.composerOwnsFocus());
        } else if (action === "quick-new-agent") {
            quickNewAgent.visible = true;
        } else if (action === "rename-agent") {
            if (app.selectedSession.length > 0 && !app.isPairSession(app.selectedSession))
                renameAgent.open(app.selectedSession, app.agentName(app.selectedSession));
        } else if (action === "new-contact") {
            quickSwitcher.openContacts(root.composerOwnsFocus());
        } else if (action.startsWith("move-")) {
            root.movePane(action.slice(5));
        } else if (action === "new") {
            root.relaunchSession = "";
            root.relaunchName = "";
            startAgent.visible = true;
        } else if (action === "overview") {
            overview.visible = true;
        } else if (action === "agent-terminal") {
            app.openAgentTerminal(app.selectedSession);
        } else if (action === "tool-narration") {
            app.toolNarrator.enabled = !app.toolNarrator.enabled;
        } else if (action === "connection") {
            connection.visible = true;
        } else if (action === "chats" || action === "updates"
                   || action === "teams" || action === "settings") {
            root.selectedSurface = action;
            if (action === "updates")
                app.loadUpdates();
            else if (action === "teams")
                app.loadTeams();
            else if (action === "chats")
                Qt.callLater(() => app.requestComposerFocus(app.panes.activePaneId));
            else if (action === "settings")
                Qt.callLater(settingsPanel.focusCurrent);
        } else if (action === "orchestrator") {
            orchestrator.visible = true;
            app.loadOrchestrator();
        } else if (action === "split-right") {
            app.panes.splitActive("vertical", app.selectedSession);
            layoutChanged = true;
        } else if (action === "split-down") {
            app.panes.splitActive("horizontal", app.selectedSession);
            layoutChanged = true;
        } else if (action === "close-pane") {
            app.panes.closePane(app.panes.activePaneId);
            layoutChanged = true;
        } else if (action === "zoom") {
            app.panes.toggleZoom();
            layoutChanged = true;
        } else if (action === "balance") {
            app.panes.equalize();
        } else if (action === "tools") {
            app.toolsVisible = !app.toolsVisible;
        } else if (action === "refresh") {
            if (root.selectedSurface === "updates") app.loadUpdates();
            else app.refreshConversation();
        } else if (action === "release-agent") {
            app.releaseAgent(app.selectedSession);
        } else if (action === "stop-agent") {
            app.stopAgent();
        } else if (action === "mute") {
            app.muted = !app.muted;
        } else if (action === "talk") {
            app.toggleRecordingForSession(app.selectedSession);
        } else if (action === "sidebar") {
            root.sidebarVisible = !root.sidebarVisible;
            Qt.callLater(() => {
                if (root.workspaceAvailable())
                    app.requestComposerFocus(app.panes.activePaneId);
            });
        } else if (action === "ui-larger") {
            root.setUiScale(root.uiScale + 0.05);
        } else if (action === "ui-smaller") {
            root.setUiScale(root.uiScale - 0.05);
        } else if (action === "ui-reset") {
            root.setUiScale(1.15);
        }
        if (layoutChanged)
            app.requestComposerFocus(app.panes.activePaneId);
    }

    AppController {
        id: app
    }
    PreviewVersions { id: previewVersions }
    readonly property bool previewCanRestart: !app.sending && !app.audio.recording && !app.audio.transcribing
    readonly property string previewUpdateLabel: {
        const catalog = previewVersions.catalog;
        if (!catalog.current || catalog.current === previewVersions.runningHash) return "";
        const version = (catalog.versions || []).find(v => v.hash === catalog.current);
        return version ? String(version.label) : "new build";
    }

    Core.Settings {
        category: "appearance"
        property alias uiScale: root.uiScale
        property alias shortcutsVisible: root.shortcutsVisible
        property alias sidebarExpandedWidth: root.sidebarExpandedWidth
        property alias redesignedSidebarSized: root.redesignedSidebarSized
    }

    onActiveChanged: {
        if (active && !root.overlayVisible())
            Qt.callLater(root.restoreSurfaceFocus);
    }

    Component.onCompleted: {
        if (!redesignedSidebarSized) {
            sidebarExpandedWidth = sidebarExpandedWidth === 232 ? 354 : Math.max(298, sidebarExpandedWidth);
            redesignedSidebarSized = true;
        }
        Qt.callLater(() => app.requestComposerFocus(app.panes.activePaneId));
    }

    function escapeFocus() {
        if (assignAgent.visible) {
            if (!assignAgent.submitting) assignAgent.closeRequested();
        } else if (launchAgent.visible) {
            if (!launchAgent.submitting) { launchAgent.autoStart = false; launchAgent.closeRequested(); }
        } else if (previewVersionPanel.visible)
            previewVersionPanel.close();
        else if (quickNewAgent.visible)
            quickNewAgent.closeRequested();
        else if (renameAgent.visible)
            renameAgent.closeRequested();
        else if (quickSwitcher.visible)
            quickSwitcher.close();
        else if (voiceDialog.visible)
            voiceDialog.visible = false;
        else if (orchestrator.visible)
            orchestrator.visible = false;
        else if (startAgent.visible)
            startAgent.visible = false;
        else if (overview.visible)
            overview.visible = false;
        else if (connection.visible && app.agents.count > 0)
            connection.visible = false;
        else if (queueDialog.visible)
            queueDialog.visible = false;
        else if (reportView.visible)
            reportView.visible = false;
        else if (profilePanel.visible)
            profilePanel.visible = false;
        else if (rail.searchOwnsFocus) {
            rail.clearSearch();
            root.runCommand("chats");
        }
        else if (root.selectedSurface !== "chats")
            root.runCommand("chats");
        else {
            app.requestComposerFocus("");
            workspace.focusNavigation();
        }
    }

    KeyboardMap {
        id: keyboard
        objectName: "keyboardMap"
        contextName: settingsPanel.dialogOpen ? "blocked" : root.overlayVisible() ? "modal"
            : root.selectedSurface !== "chats" ? root.selectedSurface
            : rail.searchOwnsFocus ? "search"
            : root.composerOwnsFocus() ? "composer"
            : rail.ownsFocus(root.activeFocusItem) ? "sidebar" : "pane"
        hasAttention: app.nextAttentionTarget.length > 0
        hasAgent: app.selectedSession.length > 0 && !app.isPairSession(app.selectedSession)
        hasRows: rail.rowCount > 0
        canSend: {
            app.composerRevision;
            return app.composerCanSend(app.panes.activePaneId, app.selectedSession);
        }
    }

    Instantiator {
        model: keyboard.shortcuts
        delegate: Shortcut {
            required property var modelData
            sequence: modelData.key
            context: Qt.WindowShortcut
            autoRepeat: modelData.action === "agent-next" || modelData.action === "agent-previous"
            onActivated: root.runCommand(modelData.action)
        }
    }

    Item {
        id: scaledSurface
        width: root.width / root.uiScale
        height: root.height / root.uiScale
        scale: root.uiScale
        transformOrigin: Item.TopLeft

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        RowLayout {
            visible: previewVersions.enabled && (root.previewUpdateLabel.length > 0
                || Boolean(previewVersions.catalog.pinned) || previewVersions.error.length > 0)
            Layout.fillWidth: true
            Layout.leftMargin: 14
            Layout.rightMargin: 14
            TuiLabel {
                text: previewVersions.error || (root.previewUpdateLabel.length > 0 ? "New update available" : "Preview pinned · automatic updates paused")
                color: previewVersions.error.length > 0 ? "#e79aa4" : "#aeb6d8"
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            TuiButton {
                visible: root.previewUpdateLabel.length > 0
                text: "Update " + root.previewUpdateLabel
                enabled: root.previewCanRestart && !previewVersions.busy
                onClicked: previewVersions.selectVersion(String(previewVersions.catalog.current))
            }
            TuiButton { text: "Versions…"; onClicked: previewVersionPanel.visible = true }
        }

        SplitView {
            id: mainSplit
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            onResizingChanged: {
                if (!resizing && root.sidebarVisible && rail.width >= 208)
                    root.sidebarExpandedWidth = rail.width;
            }

            AgentRail {
                id: rail
                objectName: "sidebarRail"
                visible: root.sidebarVisible

                SplitView.preferredWidth: root.sidebarExpandedWidth
                SplitView.minimumWidth: 298
                SplitView.maximumWidth: 494
                controller: app
                selectedSurface: root.selectedSurface
                onSelectSurface: surface => {
                    root.selectedSurface = surface;
                    if (surface === "updates")
                        app.loadUpdates();
                    else if (surface === "teams")
                        app.loadTeams();
                }
                onOpenOverview: overview.visible = true
                onOpenSwitcher: quickSwitcher.open(root.composerOwnsFocus())
                onStartAgent: root.runCommand("new")
                onHideRequested: root.runCommand("sidebar")
            }

            Item {
                objectName: "workspaceSurface"
                SplitView.fillWidth: true
                SplitView.minimumWidth: 320

                Workspace {
                    id: workspace
                    anchors.fill: parent
                    visible: root.selectedSurface === "chats"
                    controller: app
                    onOpenConnectionRequested: connection.visible = true
                    onQueueRequested: session => {
                        queueDialog.session = session;
                        queueDialog.visible = true;
                        app.loadTurnQueue(session);
                    }
                    onProfileRequested: session => {
                        app.selectSession(session);
                        profilePanel.session = session;
                        profilePanel.visible = true;
                        app.loadAgentProfile(session);
                    }
                }

                UpdatesPanel {
                    anchors.fill: parent
                    visible: root.selectedSurface === "updates"
                    controller: app
                    onOpenChat: session => {
                        app.selectSession(session);
                        root.selectedSurface = "chats";
                    }
                    onOpenReport: artifactId => reportView.open(artifactId)
                }

                TeamsPanel {
                    anchors.fill: parent
                    visible: root.selectedSurface === "teams"
                    controller: app
                    onOpenChat: session => {
                        app.selectSession(session);
                        root.selectedSurface = "chats";
                    }
                }

                SettingsPanel {
                    id: settingsPanel
                    anchors.fill: parent
                    visible: root.selectedSurface === "settings"
                    controller: app
                    onCloseRequested: root.runCommand("chats")
                    onOpenConnection: connection.visible = true
                    onOpenOrchestrator: {
                        orchestrator.visible = true;
                        app.loadOrchestrator();
                    }
                }
            }

            handle: Rectangle {
                implicitWidth: 3
                color: SplitHandle.pressed ? "#8589a4" : SplitHandle.hovered ? "#555970" : "#292b3a"

                Behavior on color {
                    ColorAnimation {
                        duration: 120
                    }

                }

            }

        }
        ShortcutBar {
            visible: root.shortcutsVisible
            Layout.fillWidth: true
            keymap: keyboard
        }
    }

    ConnectionPage {
        id: connection

        anchors.fill: parent
        controller: app
        visible: app.agents.count === 0 && app.connectionState !== "live"
        z: 100
    }

    AgentOverview {
        id: overview

        objectName: "overview"
        anchors.fill: parent
        controller: app
        visible: false
        z: 80
        onCloseRequested: visible = false
        onStartRequested: (name) => {
            root.relaunchSession = "";
            root.relaunchName = name;
            startAgent.visible = true;
        }
        onQuickStartRequested: name => {
            if (app.quickStartContact(name)) {
                overview.visible = false;
                root.selectedSurface = "chats";
            }
        }
        onRelaunchRequested: (session, name) => {
            root.relaunchSession = session;
            root.relaunchName = name;
            startAgent.visible = true;
        }
        onVoiceRequested: (session, name) => {
            root.voiceSession = session;
            root.voiceName = name;
            voiceDialog.visible = true;
            app.loadVoices(session);
        }
        onOrchestratorRequested: {
            orchestrator.visible = true;
            app.loadOrchestrator();
        }
    }

    Connections {
        target: app
        function onContactAssignmentRequested(session: string, automatic: bool) {
            if (!root.overlayVisible()) assignAgent.open(session, automatic, root.composerOwnsFocus());
        }
    }

    AssignAgentDialog {
        id: assignAgent
        objectName: "assignAgent"
        anchors.fill: parent
        controller: app
        visible: false
        z: 101
        onCloseRequested: {
            visible = false;
            if (returnToComposer) app.requestComposerFocus(app.panes.activePaneId);
            else root.focusConversation();
        }
    }

    LaunchAgentDialog {
        id: launchAgent
        objectName: "launchAgent"
        anchors.fill: parent
        controller: app
        visible: false
        z: 100
        onCloseRequested: { visible = false; root.focusConversation(); }
    }

    QuickNewAgentDialog {
        id: quickNewAgent
        objectName: "quickNewAgent"
        anchors.fill: parent
        controller: app
        visible: false
        z: 95
        onCloseRequested: {
            if (submitting && app.errorMessage.length === 0) root.selectedSurface = "chats";
            visible = false;
            root.restoreSurfaceFocus();
        }
    }

    RenameAgentDialog {
        id: renameAgent
        objectName: "renameAgent"
        anchors.fill: parent
        controller: app
        visible: false
        z: 95
        onCloseRequested: {
            visible = false;
            root.restoreSurfaceFocus();
        }
    }

    StartAgentDialog {
        id: startAgent

        objectName: "startAgent"
        anchors.fill: parent
        controller: app
        replaceSession: root.relaunchSession
        initialName: root.relaunchName
        visible: false
        z: 90
        onCloseRequested: visible = false
    }

    VoiceDialog {
        id: voiceDialog

        objectName: "voiceDialog"
        anchors.fill: parent
        controller: app
        session: root.voiceSession
        agentName: root.voiceName
        visible: false
        z: 95
        onCloseRequested: visible = false
    }

    OrchestratorDialog {
        id: orchestrator

        objectName: "orchestrator"
        anchors.fill: parent
        controller: app
        visible: false
        z: 95
        onCloseRequested: visible = false
    }

    PreviewVersionPanel {
        id: previewVersionPanel
        objectName: "previewVersionPanel"
        anchors.fill: parent
        z: 200
        switcher: previewVersions
        canRestart: root.previewCanRestart
        onClosed: Qt.callLater(() => app.requestComposerFocus(app.panes.activePaneId))
    }

    QuickSwitcher {
        id: quickSwitcher
        previewVersionsAvailable: previewVersions.enabled

        objectName: "quickSwitcher"
        anchors.fill: parent
        controller: app
        sidebarVisible: root.sidebarVisible
        visible: false
        z: 110
        onCommandRequested: action => root.runCommand(action)
        onContactRequested: name => {
            root.selectedSurface = "chats";
            app.quickStartContact(name);
        }
        onAgentRequested: session => {
            app.selectSession(session);
            root.selectedSurface = "chats";
        }
    }

    QueueDialog {
        id: queueDialog
        objectName: "queueDialog"
        anchors.fill: parent
        controller: app
        visible: false
        z: 105
        onCloseRequested: visible = false
    }

    ReportView {
        id: reportView
        anchors.fill: parent
        controller: app
        visible: false
        z: 101
        onCloseRequested: visible = false
    }

    AgentProfilePanel {
        id: profilePanel
        objectName: "agentProfilePanel"
        anchors.fill: parent
        controller: app
        visible: false
        z: 100
        onCloseRequested: visible = false
        onQueueRequested: session => {
            queueDialog.session = session;
            queueDialog.visible = true;
            app.loadTurnQueue(session);
        }
        onVoiceRequested: (session, name) => {
            root.voiceSession = session;
            root.voiceName = name;
            voiceDialog.visible = true;
            app.loadVoices(session);
        }
        onRelaunchRequested: (session, name) => {
            root.relaunchSession = session;
            root.relaunchName = name;
            startAgent.visible = true;
        }
    }

    }

}
