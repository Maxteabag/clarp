pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCore as Core
import "components"

ApplicationWindow {
    id: root

    property bool launchOnStartup: false
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
    title: newSessionHub.visible ? "New session — Clarp" : app.selectedName.length > 0 ? app.selectedName + " — Clarp" : "Clarp"
    color: Theme.window
    // Basic supplies control-specific defaults that can override the system
    // palette. Explicit window roles also propagate into popups and menus.
    // Every role follows the reading theme; the C++ side mirrors the same
    // mapping into the application palette (DesktopPalette.h).
    palette.window: Theme.window
    palette.base: Theme.window
    palette.alternateBase: Theme.raised
    palette.button: Theme.control
    palette.toolTipBase: Theme.control
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.buttonText: Theme.text
    palette.toolTipText: Theme.text
    palette.brightText: Theme.accentText
    palette.placeholderText: Theme.muted
    palette.highlight: Theme.accent
    palette.accent: Theme.accent
    palette.highlightedText: Theme.accentText
    palette.link: Theme.link
    palette.linkVisited: Theme.accent
    palette.light: Theme.light ? "#d8cbaa" : "#565b76"
    palette.midlight: Theme.border
    palette.mid: Theme.border
    palette.dark: Theme.accent
    palette.shadow: Theme.shadow
    palette.disabled.text: Theme.muted
    palette.disabled.windowText: Theme.muted
    palette.disabled.buttonText: Theme.muted

    function openLaunchAgent(backend, model, effort, anonymousMode, directory) {
        newSessionHub.openLaunch(backend, model, effort, anonymousMode, directory);
    }

    function composerOwnsFocus() {
        return root.activeFocusItem && root.activeFocusItem.objectName === "paneComposerEditor";
    }

    function overlayVisible() {
        return keymapEditor.visible || assignAgent.visible || newSessionHub.visible || previewVersionPanel.visible || renameAgent.visible || quickSwitcher.visible || voiceDialog.visible || orchestrator.visible
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
            const selectedState = app.selectedSession.length > 0
                ? app.agentState(app.selectedSession) : "";
            if (["thinking", "tool", "compacting"].includes(selectedState))
                root.runCommand("stop-agent");
            else if (keyboard.contextName === "search") rail.focusCurrentAgent();
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
        } else if (action === "change-directory") {
            newSessionHub.open(root.composerOwnsFocus(), false);
            newSessionHub.chooseDirectory();
        } else if (action === "quick-new-agent") {
            newSessionHub.open(root.composerOwnsFocus(), false);
        } else if (action === "rename-agent") {
            if (app.selectedSession.length > 0 && !app.isPairSession(app.selectedSession))
                renameAgent.open(app.selectedSession, app.agentName(app.selectedSession));
        } else if (action === "new-contact") {
            newSessionHub.open(root.composerOwnsFocus(), true);
        } else if (action.startsWith("move-")) {
            root.movePane(action.slice(5));
        } else if (action === "new") {
            newSessionHub.open(root.composerOwnsFocus(), false);
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

        } else if (action === "edit-keymap") {
            keymapEditor.visible=true;
        } else if (action === "next-workspace") {
            const items=app.panes.workspaces;const index=items.findIndex(w=>w.id===app.panes.activeWorkspace);
            if(items.length>1)app.panes.switchWorkspace(items[(index+1)%items.length].id);
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
        } else if (action === "update-preview") {
            if (previewVersions.enabled && root.previewUpdateLabel.length > 0 && root.previewCanRestart && !previewVersions.busy)
                previewVersions.selectVersion(String(previewVersions.catalog.current));
            else if (previewVersions.enabled) previewVersionPanel.visible = true;
        } else if (action === "jump-latest") {
            workspace.jumpToLatest();
        } else if (action === "retry-message") {
            app.retryLatestFailedMessage();
        } else if (action === "dismiss-error") {
            root.dismissConversationError();
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

    // The keymap editor is built on first open: it cost ~0.2 s of every
    // launch and is rarely used. The host keeps the old id, visibility and
    // closed() contract, so callers are unchanged.
    Item {
        id: keymapEditor
        objectName: "keymapEditorHost"
        anchors.fill: parent
        z: 300
        visible: false
        signal closed()
        onClosed: app.requestComposerFocus(app.panes.activePaneId)
        onVisibleChanged: {
            if (visible) keymapEditorLoader.active = true;
            if (keymapEditorLoader.item) (keymapEditorLoader.item as Item).visible = visible;
        }
        Loader {
            id: keymapEditorLoader
            anchors.fill: parent
            active: false
            // By URL, like DeferredPanel, so the editor's type loads on first open.
            onActiveChanged: if (active) setSource(Qt.resolvedUrl("components/KeymapEditor.qml"), { objectName: "keymapEditor", keymap: keyboard })
            // Shown after load so the editor's own onVisibleChanged fills and focuses it.
            onLoaded: {
                const editor = item as QtObject;
                const closedSignal = "closed";
                editor[closedSignal].connect(() => { keymapEditor.visible = false; keymapEditor.closed(); });
                (item as Item).visible = keymapEditor.visible;
            }
        }
    }
    AppController {
        id: app
        Component.onCompleted: Theme.style = Qt.binding(() => app.readingStyle)
    }
    PreviewVersions {
        id: previewVersions
        restartAllowed: root.previewCanRestart
        selectedSession: app.selectedSession
        selectedHost: app.baseUrl
    }
    readonly property bool previewCanRestart: !app.sending && !app.uploading && !app.audio.recording && !app.audio.transcribing && !app.audio.playing
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
    // The sidebar starts shown on a first launch and then follows the last
    // Ctrl+B choice. Not an alias: the launch can force it open
    // (--no-new-agent), and that must not overwrite the saved choice.
    Core.Settings {
        id: sidebarSetting
        category: "appearance"
        property bool sidebarVisible: true
    }
    property bool sidebarRestored: false
    onSidebarVisibleChanged: if (sidebarRestored) sidebarSetting.sidebarVisible = sidebarVisible

    onActiveChanged: {
        if (active && !root.overlayVisible())
            Qt.callLater(root.restoreSurfaceFocus);
    }

    Component.onCompleted: {
        if (!root.sidebarVisible) root.sidebarVisible = sidebarSetting.sidebarVisible;
        root.sidebarRestored = true;
        app.markStartup("main-qml-completed");
        if (!redesignedSidebarSized) {
            sidebarExpandedWidth = sidebarExpandedWidth === 232 ? 354 : Math.max(298, sidebarExpandedWidth);
            redesignedSidebarSized = true;
        }
        Qt.callLater(() => app.requestComposerFocus(app.panes.activePaneId));
    }

    function dismissConversationError() {
        if (root.selectedSurface !== "chats") return false;
        const model = app.selectedSession.length > 0
            ? app.conversationForSession(app.selectedSession) : null;
        if (app.errorMessage.length === 0 && (!model || (model.error.length === 0 && model.voiceError.length === 0)))
            return false;
        app.clearError();
        if (model) { model.error = ""; model.voiceError = ""; }
        return true;
    }

    function escapeFocus() {
        if(keymapEditor.visible){keymapEditor.visible=false;keymapEditor.closed();return;}
        if (assignAgent.visible) {
            if (!assignAgent.submitting) assignAgent.closeRequested();
        } else if (newSessionHub.visible) {
            if (!newSessionHub.submitting) newSessionHub.stepBack();
        } else if (previewVersionPanel.visible)
            previewVersionPanel.close();
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
        else if (root.dismissConversationError())
            return;
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
        contextName: settingsPanel.dialogOpen ? "blocked"
            : newSessionHub.visible && !quickSwitcher.visible ? "launch" : root.overlayVisible() ? "modal"
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
            enabled: !(newSessionHub.visible && modelData.action === "escape")
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
        id: desktopShell
        objectName: "desktopShell"
        visible: true
        anchors.fill: parent
        spacing: 0

        Loader {
            active: previewVersions.enabled && (root.previewUpdateLabel.length > 0
                || Boolean(previewVersions.catalog.pinned) || previewVersions.error.length > 0)
            visible: active
            Layout.fillWidth: true
            Layout.leftMargin: 14
            Layout.rightMargin: 14
            sourceComponent: RowLayout {
                TuiLabel {
                    text: previewVersions.error || (root.previewUpdateLabel.length > 0 ? "New update available" : "Preview pinned · automatic updates paused")
                    color: previewVersions.error.length > 0 ? Theme.danger : Theme.secondary
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
                TuiButton {
                    visible: root.previewUpdateLabel.length > 0
                    text: "Update " + root.previewUpdateLabel + " · Ctrl+Alt+U"
                    enabled: root.previewCanRestart && !previewVersions.busy
                    onClicked: previewVersions.selectVersion(String(previewVersions.catalog.current))
                }
                TuiButton { text: "Versions…"; onClicked: previewVersionPanel.visible = true }
            }
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
                    enabled: root.workspaceAvailable()
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

                DeferredPanel {
                    objectName: "updatesPanel"
                    anchors.fill: parent
                    visible: root.selectedSurface === "updates"
                    url: Qt.resolvedUrl("components/UpdatesPanel.qml")
                    properties: ({ controller: app })
                    handlers: ({
                        openChat: session => {
                            app.selectSession(session);
                            root.selectedSurface = "chats";
                        },
                        openReport: artifactId => reportView.open(artifactId)
                    })
                }

                DeferredPanel {
                    objectName: "teamsPanel"
                    anchors.fill: parent
                    visible: root.selectedSurface === "teams"
                    url: Qt.resolvedUrl("components/TeamsPanel.qml")
                    properties: ({ controller: app })
                    handlers: ({
                        openChat: session => {
                            app.selectSession(session);
                            root.selectedSurface = "chats";
                        }
                    })
                }

                DeferredPanel {
                    id: settingsPanel
                    objectName: "settingsPanel"
                    readonly property bool dialogOpen: value("dialogOpen", false)
                    function focusCurrent() { call("focusCurrent"); }
                    anchors.fill: parent
                    visible: root.selectedSurface === "settings"
                    url: Qt.resolvedUrl("components/SettingsPanel.qml")
                    properties: ({ controller: app })
                    handlers: ({
                        closeRequested: () => root.runCommand("chats"),
                        openConnection: () => { connection.visible = true; },
                        openOrchestrator: () => {
                            orchestrator.visible = true;
                            app.loadOrchestrator();
                        }
                    })
                }
            }

            handle: Rectangle {
                implicitWidth: 3
                color: SplitHandle.pressed ? Theme.secondary : SplitHandle.hovered ? Theme.faint : Theme.control

                Behavior on color {
                    ColorAnimation {
                        duration: 120
                    }

                }

            }

        }
        // Hidden bars are not built until shown (18 items when turned off).
        Loader {
            active: root.shortcutsVisible
            visible: active
            Layout.fillWidth: true
            sourceComponent: ShortcutBar {
                keymap: keyboard
            }
        }
    }

    // Hidden whenever the cached roster has agents, so most starts never build it.
    DeferredPanel {
        id: connection

        anchors.fill: parent
        visible: !newSessionHub.visible && app.agents.count === 0 && app.connectionState !== "live"
        z: 100
        url: Qt.resolvedUrl("components/ConnectionPage.qml")
        properties: ({ controller: app })
    }

    DeferredPanel {
        id: overview
        objectName: "overview"

        anchors.fill: parent
        z: 80
        url: Qt.resolvedUrl("components/AgentOverview.qml")
        properties: ({ controller: app })
        handlers: ({
            closeRequested: () => { overview.visible = false; },
            startRequested: name => {
                root.relaunchSession = "";
                root.relaunchName = name;
                startAgent.visible = true;
            },
            quickStartRequested: name => {
                if (app.quickStartContact(name)) {
                    overview.visible = false;
                    root.selectedSurface = "chats";
                }
            },
            relaunchRequested: (session, name) => {
                root.relaunchSession = session;
                root.relaunchName = name;
                startAgent.visible = true;
            },
            voiceRequested: (session, name) => {
                root.voiceSession = session;
                root.voiceName = name;
                voiceDialog.visible = true;
                app.loadVoices(session);
            },
            orchestratorRequested: () => {
                orchestrator.visible = true;
                app.loadOrchestrator();
            }
        })
    }

    Connections {
        target: app
        function onContactAssignmentRequested(session: string, automatic: bool) {
            if (!root.overlayVisible()) assignAgent.open(session, automatic, root.composerOwnsFocus());
        }
    }

    DeferredPanel {
        id: assignAgent
        objectName: "assignAgent"
        readonly property bool submitting: value("submitting", false)
        function open(target, automatic, restoreComposer) {
            visible = true;
            call("open", target, automatic, restoreComposer);
        }
        function closeRequested() { call("closeRequested"); }
        anchors.fill: parent
        z: 101
        url: Qt.resolvedUrl("components/AssignAgentDialog.qml")
        properties: ({ controller: app })
        handlers: ({
            closeRequested: () => {
                assignAgent.visible = false;
                if (assignAgent.value("returnToComposer", false)) app.requestComposerFocus(app.panes.activePaneId);
                else root.focusConversation();
            }
        })
    }

    // Built on first open (72 text items and two combo boxes most starts never
    // use). The loader mirrors the hub's own open state as its visibility.
    Loader {
        id: newSessionHub
        objectName: "newSessionHub"
        // Loaded by URL for the reason given in DeferredPanel.qml; the hub's
        // type is unknown here, so its members are reached by name.
        readonly property bool submitting: Boolean(member("submitting"))
        function member(name) {
            const loaded = item as QtObject;
            return loaded === null ? undefined : loaded[name];
        }
        function hub() {
            if (item === null) {
                active = true;
                setSource(Qt.resolvedUrl("components/NewSessionHub.qml"), { controller: app });
                const handlers = {
                    connectionRequested: () => { connection.visible = true; },
                    closeRequested: () => {
                        root.selectedSurface = "chats";
                        root.restoreSurfaceFocus();
                    }
                };
                for (const name in handlers)
                    member(name).connect(handlers[name]);
            }
            return item as QtObject;
        }
        function invoke(method, ...args) { return hub()[method](...args); }
        function open(returnToComposer, contactMode) { invoke("open", returnToComposer, contactMode); }
        function openLaunch(backend, model, effort, anonymousMode, directory) {
            invoke("openLaunch", backend, model, effort, anonymousMode, directory);
        }
        function stepBack() { if (item !== null) invoke("stepBack"); }
        function chooseDirectory() {
            const property = "choosingDirectory";
            hub()[property] = true;
        }
        anchors.fill: parent
        z: 100
        active: false
        visible: Boolean(member("shown"))
        Component.onCompleted: if (root.launchOnStartup) newSessionHub.open(false, false)
    }

    DeferredPanel {
        id: renameAgent
        objectName: "renameAgent"
        // Screenshot runs set these on the loader before showing it.
        property string session
        property string currentName
        function open(session, name) {
            renameAgent.session = session;
            renameAgent.currentName = name;
            visible = true;
        }
        function closeRequested() { call("closeRequested"); }
        anchors.fill: parent
        z: 95
        url: Qt.resolvedUrl("components/RenameAgentDialog.qml")
        properties: ({ controller: app, session: renameAgent.session, currentName: renameAgent.currentName })
        handlers: ({
            closeRequested: () => {
                renameAgent.visible = false;
                root.restoreSurfaceFocus();
            }
        })
        Binding { target: renameAgent.item; property: "session"; value: renameAgent.session; when: renameAgent.item !== null }
        Binding { target: renameAgent.item; property: "currentName"; value: renameAgent.currentName; when: renameAgent.item !== null }
    }

    DeferredPanel {
        id: startAgent
        objectName: "startAgent"
        anchors.fill: parent
        z: 90
        url: Qt.resolvedUrl("components/StartAgentDialog.qml")
        properties: ({ controller: app, replaceSession: root.relaunchSession, initialName: root.relaunchName })
        handlers: ({ closeRequested: () => { startAgent.visible = false; } })
        Binding { target: startAgent.item; property: "replaceSession"; value: root.relaunchSession; when: startAgent.item !== null }
        Binding { target: startAgent.item; property: "initialName"; value: root.relaunchName; when: startAgent.item !== null }
    }

    DeferredPanel {
        id: voiceDialog
        objectName: "voiceDialog"
        anchors.fill: parent
        z: 95
        url: Qt.resolvedUrl("components/VoiceDialog.qml")
        properties: ({ controller: app, session: root.voiceSession, agentName: root.voiceName })
        handlers: ({ closeRequested: () => { voiceDialog.visible = false; } })
        Binding { target: voiceDialog.item; property: "session"; value: root.voiceSession; when: voiceDialog.item !== null }
        Binding { target: voiceDialog.item; property: "agentName"; value: root.voiceName; when: voiceDialog.item !== null }
    }

    DeferredPanel {
        id: orchestrator
        objectName: "orchestrator"
        anchors.fill: parent
        z: 95
        url: Qt.resolvedUrl("components/OrchestratorDialog.qml")
        properties: ({ controller: app })
        handlers: ({ closeRequested: () => { orchestrator.visible = false; } })
    }

    DeferredPanel {
        id: previewVersionPanel
        objectName: "previewVersionPanel"
        function close() { call("close"); }
        anchors.fill: parent
        z: 200
        url: Qt.resolvedUrl("components/PreviewVersionPanel.qml")
        properties: ({ switcher: previewVersions, canRestart: root.previewCanRestart })
        handlers: ({ closed: () => Qt.callLater(() => app.requestComposerFocus(app.panes.activePaneId)) })
        Binding { target: previewVersionPanel.item; property: "canRestart"; value: root.previewCanRestart; when: previewVersionPanel.item !== null }
    }

    DeferredPanel {
        id: quickSwitcher
        objectName: "quickSwitcher"
        // Screenshot runs set the query on the loader before showing it.
        property string query
        onQueryChanged: if (item !== null) (item as QtObject)["query"] = query
        function open(returnToComposer) {
            visible = true;
            call("open", returnToComposer);
        }
        function openContacts(returnToComposer) {
            visible = true;
            call("openContacts", returnToComposer);
        }
        function close(restoreFocus) { call("close", restoreFocus); }
        anchors.fill: parent
        z: 110
        url: Qt.resolvedUrl("components/QuickSwitcher.qml")
        properties: ({
            controller: app,
            query: quickSwitcher.query,
            previewVersionsAvailable: previewVersions.enabled,
            sidebarVisible: root.sidebarVisible
        })
        handlers: ({
            commandRequested: action => root.runCommand(action),
            contactRequested: name => {
                root.selectedSurface = "chats";
                app.quickStartContact(name);
            },
            agentRequested: session => {
                app.selectSession(session);
                root.selectedSurface = "chats";
            }
        })
        Binding { target: quickSwitcher.item; property: "previewVersionsAvailable"; value: previewVersions.enabled; when: quickSwitcher.item !== null }
        Binding { target: quickSwitcher.item; property: "sidebarVisible"; value: root.sidebarVisible; when: quickSwitcher.item !== null }
    }

    DeferredPanel {
        id: queueDialog
        objectName: "queueDialog"
        property string session
        anchors.fill: parent
        z: 105
        url: Qt.resolvedUrl("components/QueueDialog.qml")
        properties: ({ controller: app, session: queueDialog.session })
        handlers: ({ closeRequested: () => { queueDialog.visible = false; } })
        Binding { target: queueDialog.item; property: "session"; value: queueDialog.session; when: queueDialog.item !== null }
    }

    DeferredPanel {
        id: reportView
        objectName: "reportView"
        function open(artifactId) {
            visible = true;
            call("open", artifactId);
        }
        anchors.fill: parent
        z: 101
        url: Qt.resolvedUrl("components/ReportView.qml")
        properties: ({ controller: app })
        handlers: ({ closeRequested: () => { reportView.visible = false; } })
    }

    DeferredPanel {
        id: profilePanel
        objectName: "agentProfilePanel"
        property string session
        anchors.fill: parent
        z: 100
        url: Qt.resolvedUrl("components/AgentProfilePanel.qml")
        properties: ({ controller: app, session: profilePanel.session })
        handlers: ({
            closeRequested: () => { profilePanel.visible = false; },
            queueRequested: session => {
                queueDialog.session = session;
                queueDialog.visible = true;
                app.loadTurnQueue(session);
            },
            voiceRequested: (session, name) => {
                root.voiceSession = session;
                root.voiceName = name;
                voiceDialog.visible = true;
                app.loadVoices(session);
            },
            relaunchRequested: (session, name) => {
                root.relaunchSession = session;
                root.relaunchName = name;
                startAgent.visible = true;
            }
        })
        Binding { target: profilePanel.item; property: "session"; value: profilePanel.session; when: profilePanel.item !== null }
    }

    }

}
