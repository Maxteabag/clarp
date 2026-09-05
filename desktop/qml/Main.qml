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
    property bool sidebarVisible: true
    property real sidebarExpandedWidth: 354
    property bool redesignedSidebarSized: false
    readonly property bool settingsOverlayVisible: root.selectedSurface === "settings" && root.overlayVisible()

    width: 1360
    height: 900
    minimumWidth: Math.max(760, sidebarVisible ? Math.ceil(624 * uiScale) : 760)
    minimumHeight: 520
    visible: true
    title: app.selectedName.length > 0 ? app.selectedName + " — Clarp" : "Clarp"
    color: "#121116"

    function composerOwnsFocus() {
        return root.activeFocusItem && root.activeFocusItem.objectName === "paneComposerEditor";
    }

    function overlayVisible() {
        return quickSwitcher.visible || voiceDialog.visible || orchestrator.visible
            || startAgent.visible || overview.visible || connection.visible
            || queueDialog.visible || profilePanel.visible || settingsPanel.dialogOpen;
    }

    function workspaceAvailable() {
        return root.selectedSurface === "chats" && !root.overlayVisible();
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
        let layoutChanged = false;
        if (action === "new") {
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
            app.refreshConversation();
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

    Core.Settings {
        category: "appearance"
        property alias uiScale: root.uiScale
        property alias sidebarVisible: root.sidebarVisible
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

    palette {
        window: "#121116"
        windowText: "#e9e4df"
        base: "#17151c"
        alternateBase: "#1d1a22"
        text: "#e9e4df"
        button: "#211e28"
        buttonText: "#e9e4df"
        highlight: "#b884d8"
        highlightedText: "#171821"
        placeholderText: "#5f6278"
    }

    Shortcut {
        sequence: "Ctrl+R"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: root.selectedSurface === "updates"
            ? app.loadUpdates() : app.refreshConversation()
    }

    Shortcut {
        sequence: "Ctrl+M"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: app.muted = !app.muted
    }

    Shortcut {
        sequence: "Ctrl+K"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: quickSwitcher.open(root.composerOwnsFocus())
    }

    Shortcut {
        sequence: "Ctrl+B"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: root.runCommand("sidebar")
    }

    Shortcut {
        sequence: "Ctrl+="
        context: Qt.ApplicationShortcut
        onActivated: root.runCommand("ui-larger")
    }

    Shortcut {
        sequence: "Ctrl+-"
        context: Qt.ApplicationShortcut
        onActivated: root.runCommand("ui-smaller")
    }

    Shortcut {
        sequence: "Ctrl+0"
        context: Qt.ApplicationShortcut
        onActivated: root.runCommand("ui-reset")
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.ApplicationShortcut
        // Let the settings dialog (and its ComboBox popup) consume Escape first.
        enabled: !settingsPanel.dialogOpen
        onActivated: {
            if (quickSwitcher.visible)
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
                workspace.forceActiveFocus();
            }
        }
    }

    Shortcut {
        sequence: "Ctrl+Shift+V"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable() && !root.composerOwnsFocus()
        onActivated: root.runCommand("split-right")
    }

    Shortcut {
        sequence: "Ctrl+Shift+H"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable() && !root.composerOwnsFocus()
        onActivated: root.runCommand("split-down")
    }

    Shortcut {
        sequence: "Ctrl+Shift+W"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable() && !root.composerOwnsFocus()
        onActivated: root.runCommand("close-pane")
    }

    Shortcut {
        sequence: "Ctrl+Shift+Z"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable() && !root.composerOwnsFocus()
        onActivated: root.runCommand("zoom")
    }

    Shortcut {
        sequence: "Ctrl+Shift+="
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable() && !root.composerOwnsFocus()
        onActivated: app.panes.equalize()
    }

    Shortcut {
        sequence: "Ctrl+Alt+Left"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable()
        onActivated: root.movePane("left")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Right"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable()
        onActivated: root.movePane("right")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Up"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable()
        onActivated: root.movePane("up")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Down"
        context: Qt.ApplicationShortcut
        enabled: root.workspaceAvailable()
        onActivated: root.movePane("down")
    }

    Shortcut {
        sequence: "Ctrl+N"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: {
            root.relaunchSession = "";
            root.relaunchName = "";
            startAgent.visible = true;
        }
    }

    Shortcut {
        sequence: "Ctrl+Shift+O"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: overview.visible = true
    }

    Shortcut {
        sequence: "Ctrl+Shift+T"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: app.toolsVisible = !app.toolsVisible
    }

    Shortcut {
        sequence: "Ctrl+,"
        context: Qt.ApplicationShortcut
        enabled: !root.overlayVisible()
        onActivated: root.runCommand("settings")
    }

    Shortcut { sequence: "Ctrl+1"; context: Qt.ApplicationShortcut; enabled: !root.overlayVisible(); onActivated: root.runCommand("chats") }
    Shortcut { sequence: "Ctrl+2"; context: Qt.ApplicationShortcut; enabled: !root.overlayVisible(); onActivated: root.runCommand("updates") }
    Shortcut { sequence: "Ctrl+3"; context: Qt.ApplicationShortcut; enabled: !root.overlayVisible(); onActivated: root.runCommand("teams") }
    Shortcut { sequence: "Ctrl+4"; context: Qt.ApplicationShortcut; enabled: !root.overlayVisible(); onActivated: root.runCommand("settings") }

    Shortcut { sequence: "Alt+Left"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.movePane("left") }
    Shortcut { sequence: "Alt+Right"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.movePane("right") }
    Shortcut { sequence: "Alt+Up"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.movePane("up") }
    Shortcut { sequence: "Alt+Down"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.movePane("down") }
    Shortcut { sequence: "Alt+V"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.runCommand("split-right") }
    Shortcut { sequence: "Alt+S"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.runCommand("split-down") }
    Shortcut { sequence: "Alt+X"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.runCommand("close-pane") }
    Shortcut { sequence: "Alt+Z"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.runCommand("zoom") }
    Shortcut { sequence: "Alt+="; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable() && !root.composerOwnsFocus(); onActivated: root.runCommand("balance") }
    Shortcut { sequence: "Ctrl+Alt+V"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("split-right") }
    Shortcut { sequence: "Ctrl+Alt+S"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("split-down") }
    Shortcut { sequence: "Ctrl+Alt+X"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("close-pane") }
    Shortcut { sequence: "Ctrl+Alt+T"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("agent-terminal") }
    Shortcut { sequence: "Ctrl+Alt+N"; context: Qt.ApplicationShortcut; enabled: !root.overlayVisible(); onActivated: quickSwitcher.openContacts(root.composerOwnsFocus()) }
    Shortcut { sequence: "Ctrl+Alt+Z"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("zoom") }
    Shortcut { sequence: "Ctrl+Alt+="; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("balance") }
    Shortcut { sequence: "Ctrl+."; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("stop-agent") }
    Shortcut { sequence: "Ctrl+Shift+Space"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("talk") }

    Item {
        id: scaledSurface
        width: root.width / root.uiScale
        height: root.height / root.uiScale
        scale: root.uiScale
        transformOrigin: Item.TopLeft

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

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

    QuickSwitcher {
        id: quickSwitcher

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
