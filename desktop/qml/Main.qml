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

    width: 1360
    height: 900
    minimumWidth: 760
    minimumHeight: 520
    visible: true
    title: app.selectedName.length > 0 ? app.selectedName + " — Clarp" : "Clarp"
    color: "#121116"

    AppController {
        id: app
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
        highlightedText: "#120f16"
        placeholderText: "#77717f"
    }

    Shortcut {
        sequence: "Ctrl+R"
        onActivated: app.refreshConversation()
    }

    Shortcut {
        sequence: "Ctrl+M"
        onActivated: app.muted = !app.muted
    }

    Shortcut {
        sequence: "Ctrl+K"
        onActivated: quickSwitcher.open()
    }

    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (quickSwitcher.visible)
                quickSwitcher.visible = false;
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
        }
    }

    Shortcut {
        sequence: "Ctrl+Shift+V"
        onActivated: app.panes.splitActive("vertical", app.selectedSession)
    }

    Shortcut {
        sequence: "Ctrl+Shift+H"
        onActivated: app.panes.splitActive("horizontal", app.selectedSession)
    }

    Shortcut {
        sequence: "Ctrl+Shift+W"
        onActivated: app.panes.closePane(app.panes.activePaneId)
    }

    Shortcut {
        sequence: "Ctrl+Shift+Z"
        onActivated: app.panes.toggleZoom()
    }

    Shortcut {
        sequence: "Ctrl+Shift+="
        onActivated: app.panes.equalize()
    }

    Shortcut {
        sequence: "Ctrl+Alt+Left"
        onActivated: app.panes.navigate("left")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Right"
        onActivated: app.panes.navigate("right")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Up"
        onActivated: app.panes.navigate("up")
    }

    Shortcut {
        sequence: "Ctrl+Alt+Down"
        onActivated: app.panes.navigate("down")
    }

    Shortcut {
        sequence: "Ctrl+N"
        onActivated: {
            root.relaunchSession = "";
            root.relaunchName = "";
            startAgent.visible = true;
        }
    }

    Shortcut {
        sequence: "Ctrl+Shift+O"
        onActivated: overview.visible = true
    }

    Shortcut {
        sequence: "Ctrl+Shift+T"
        onActivated: app.toolsVisible = !app.toolsVisible
    }

    Shortcut {
        sequence: "Ctrl+,"
        onActivated: connection.visible = true
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal

            AgentRail {
                id: rail

                SplitView.preferredWidth: collapsed ? 68 : 278
                SplitView.minimumWidth: collapsed ? 68 : 218
                SplitView.maximumWidth: collapsed ? 68 : 430
                controller: app
                onOpenOverview: overview.visible = true
            }

            Workspace {
                SplitView.fillWidth: true
                SplitView.minimumWidth: 520
                controller: app
                onOpenConnectionRequested: connection.visible = true
            }

            handle: Rectangle {
                implicitWidth: 5
                color: SplitHandle.pressed ? "#b884d8" : SplitHandle.hovered ? "#594765" : "#24202b"

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
    }

}
