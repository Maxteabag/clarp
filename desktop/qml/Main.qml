import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"

ApplicationWindow {
    id: root

    property string relaunchSession: ""
    property string relaunchName: ""
    property string voiceSession: ""
    property string voiceName: ""
    property string selectedSurface: "chats"

    width: 1360
    height: 900
    minimumWidth: 760
    minimumHeight: 520
    visible: true
    title: app.selectedName.length > 0 ? app.selectedName + " — Clarp" : "Clarp"
    color: "#1a1b26"

    function composerOwnsFocus() {
        return root.activeFocusItem && root.activeFocusItem.objectName === "paneComposerEditor";
    }

    function overlayVisible() {
        return quickSwitcher.visible || voiceDialog.visible || orchestrator.visible
            || startAgent.visible || overview.visible || connection.visible
            || queueDialog.visible || profilePanel.visible;
    }

    function workspaceAvailable() {
        return root.selectedSurface === "chats" && !root.overlayVisible();
    }

    function movePane(direction) {
        const keepComposer = root.composerOwnsFocus();
        app.panes.navigate(direction);
        if (keepComposer)
            app.requestComposerFocus(app.panes.activePaneId);
    }

    function runCommand(action) {
        const keepComposer = root.composerOwnsFocus();
        let layoutChanged = false;
        if (action === "new") {
            root.relaunchSession = "";
            root.relaunchName = "";
            startAgent.visible = true;
        } else if (action === "overview") {
            overview.visible = true;
        } else if (action === "connection") {
            connection.visible = true;
        } else if (action === "chats" || action === "updates"
                   || action === "teams" || action === "settings") {
            root.selectedSurface = action;
            if (action === "updates")
                app.loadUpdates();
            else if (action === "teams")
                app.loadTeams();
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
            app.audio.toggleRecording();
        }
        if (keepComposer && layoutChanged)
            app.requestComposerFocus(app.panes.activePaneId);
    }

    AppController {
        id: app
    }

    palette {
        window: "#1a1b26"
        windowText: "#c6c8dc"
        base: "#1c1d28"
        alternateBase: "#242532"
        text: "#c6c8dc"
        button: "#20212d"
        buttonText: "#b6b9cf"
        highlight: "#8f93ae"
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
        sequence: "Escape"
        context: Qt.ApplicationShortcut
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
            else if (root.selectedSurface !== "chats")
                root.selectedSurface = "chats";
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
        onActivated: root.selectedSurface = "settings"
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
    Shortcut { sequence: "Ctrl+Alt+Z"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("zoom") }
    Shortcut { sequence: "Ctrl+Alt+="; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("balance") }
    Shortcut { sequence: "Ctrl+."; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("stop-agent") }
    Shortcut { sequence: "Ctrl+Shift+Space"; context: Qt.ApplicationShortcut; enabled: root.workspaceAvailable(); onActivated: root.runCommand("talk") }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal

            AgentRail {
                id: rail

                SplitView.preferredWidth: collapsed ? 44 : 224
                SplitView.minimumWidth: collapsed ? 44 : 176
                SplitView.maximumWidth: collapsed ? 44 : 320
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
            }

            Item {
                SplitView.fillWidth: true
                SplitView.minimumWidth: 520

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
                    anchors.fill: parent
                    visible: root.selectedSurface === "settings"
                    controller: app
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
            Layout.fillWidth: true
            controller: app
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
        visible: false
        z: 110
        onCommandRequested: action => root.runCommand(action)
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
