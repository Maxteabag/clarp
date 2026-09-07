pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string replaceSession: ""
    property string initialName: ""
    property string launchMode: "fresh"
    property string pastSessionId: ""
    property var selectedMcpServers: []
    signal closeRequested

    color: "#e61a1b26"

    function selectValue(combo, value) {
        const index = combo.indexOfValue(value);
        if (index >= 0)
            combo.currentIndex = index;
    }

    function pathLabel(path) {
        const value = String(path);
        const trimmed = value.endsWith("/") ? value.slice(0, -1) : value;
        const slash = trimmed.lastIndexOf("/");
        return slash >= 0 ? trimmed.slice(slash + 1) : trimmed;
    }

    function chooseWorkspace(path) {
        workspaceField.text = String(path);
        root.controller.loadDirectorySuggestions(workspaceField.text);
        if (root.launchMode !== "fresh")
            root.controller.loadPastSessions(workspaceField.text, String(backendField.currentValue));
    }

    function setLaunchMode(mode) {
        root.launchMode = mode;
        root.pastSessionId = "";
        if (mode !== "fresh")
            root.controller.loadPastSessions(workspaceField.text, String(backendField.currentValue));
    }

    onVisibleChanged: {
        if (!visible)
            return;
        nameField.text = initialName;
        selectedMcpServers = replaceSession.length > 0
            ? Array.from(root.controller.agentDetails(replaceSession).mcp_servers || []) : [];
        setLaunchMode("fresh");
        workspaceField.text = replaceSession.length > 0 ? root.controller.agentWorkingDirectory(replaceSession) : root.controller.lastWorkingDirectory;
        if (workspaceField.text.length === 0)
            workspaceField.text = "~";
        root.controller.loadFavoritePaths();
        root.controller.loadDirectorySuggestions(workspaceField.text);
        const wantedBackend = replaceSession.length > 0 ? root.controller.agentBackend(replaceSession) : root.controller.lastBackend;
        Qt.callLater(() => {
            root.selectValue(backendField, wantedBackend);
            if (replaceSession.length === 0)
                return;
            root.selectValue(modelField, root.controller.agentModel(replaceSession));
            Qt.callLater(() => root.selectValue(effortField, root.controller.agentEffort(replaceSession)));
        });
    }

    Connections {
        target: root.controller
        function onAgentMutationSucceeded() {
            root.closeRequested();
        }
    }

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Timer {
        id: suggestionTimer
        interval: 220
        onTriggered: {
            root.controller.loadDirectorySuggestions(workspaceField.text);
            if (root.launchMode !== "fresh")
                root.controller.loadPastSessions(workspaceField.text, String(backendField.currentValue));
        }
    }

    Rectangle {
        width: Math.min(620, parent.width - 48)
        height: Math.min(760, parent.height - 48)
        anchors.centerIn: parent
        radius: 20
        color: "#20212e"
        border.color: "#41445a"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 10

            ScrollView {
                id: formScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: formScroll.availableWidth
                    spacing: 12

                Text {
                    text: root.replaceSession.length > 0 ? "Relaunch agent" : "Start an agent"
                    color: "#c0caf5"
                    font.pixelSize: 21
                    font.weight: Font.DemiBold
                }

                Label {
                    text: "Name"
                    color: "#8d93b0"
                }
                TextField {
                    id: nameField
                    Layout.fillWidth: true
                    placeholderText: "Rachel"
                    selectByMouse: true
                }

                Label {
                    text: "Workspace"
                    color: "#8d93b0"
                }
                TextField {
                    id: workspaceField
                    Layout.fillWidth: true
                    placeholderText: "~/Projects/example"
                    selectByMouse: true
                    onTextEdited: suggestionTimer.restart()
                }

                ListView {
                    visible: count > 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? 32 : 0
                    orientation: ListView.Horizontal
                    spacing: 5
                    clip: true
                    model: root.controller.directorySuggestions

                    delegate: Button {
                        required property var modelData
                        text: root.pathLabel(modelData)
                        ToolTip.visible: hovered
                        ToolTip.text: String(modelData)
                        onClicked: root.chooseWorkspace(modelData)
                    }
                }

                ListView {
                    visible: count > 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? 32 : 0
                    orientation: ListView.Horizontal
                    spacing: 5
                    clip: true
                    model: root.controller.favoritePaths

                    delegate: Button {
                        id: favoriteButton
                        required property var modelData
                        readonly property string favoritePath: String(modelData.path || "")
                        text: "★ " + root.pathLabel(favoritePath)
                        ToolTip.visible: hovered
                        ToolTip.text: favoritePath + (Number(modelData.use_count || 0) > 1 ? " · " + Number(modelData.use_count) + " launches" : "")
                        onClicked: root.chooseWorkspace(favoritePath)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10

                    ColumnLayout {
                        Layout.fillWidth: true
                        Label {
                            text: "Backend"
                            color: "#8d93b0"
                        }
                        ThemedComboBox {
                            id: backendField
                            Layout.fillWidth: true
                            model: root.controller.backendOptions
                            textRole: "label"
                            valueRole: "id"
                            onActivated: root.setLaunchMode("fresh")
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Label {
                            text: "Effort"
                            color: "#8d93b0"
                        }
                        ThemedComboBox {
                            id: effortField
                            Layout.fillWidth: true
                            model: root.controller.effortsForModel(String(backendField.currentValue), String(modelField.currentValue))
                            textRole: "label"
                            valueRole: "id"
                        }
                    }
                }

                Label {
                    text: "Model"
                    color: "#8d93b0"
                }
                ThemedComboBox {
                    id: modelField
                    Layout.fillWidth: true
                    model: root.controller.modelsForBackend(String(backendField.currentValue))
                    textRole: "label"
                    valueRole: "id"
                }

                ColumnLayout {
                    visible: String(backendField.currentValue) === "claude"
                        && root.controller.availableMcpServers.length > 0
                    Layout.fillWidth: true
                    spacing: 4
                    Label {
                        text: "MCP servers"
                        color: "#8d93b0"
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 6
                        Repeater {
                            model: root.controller.availableMcpServers
                            delegate: CheckBox {
                                id: mcpServer
                                required property string modelData
                                text: modelData
                                checked: root.selectedMcpServers.includes(modelData)
                                onToggled: {
                                    const selected = Array.from(root.selectedMcpServers);
                                    const index = selected.indexOf(modelData);
                                    if (checked && index < 0)
                                        selected.push(modelData);
                                    else if (!checked && index >= 0)
                                        selected.splice(index, 1);
                                    root.selectedMcpServers = selected;
                                }
                            }
                        }
                    }
                }

                Label {
                    text: "Conversation"
                    color: "#8d93b0"
                }
                RowLayout {
                    Layout.fillWidth: true
                    Button {
                        text: "Fresh"
                        checked: root.launchMode === "fresh"
                        checkable: true
                        onClicked: root.setLaunchMode("fresh")
                    }
                    Button {
                        visible: root.controller.backendSupportsResume(String(backendField.currentValue))
                        text: "Resume"
                        checked: root.launchMode === "resume"
                        checkable: true
                        onClicked: root.setLaunchMode("resume")
                    }
                    Button {
                        visible: root.controller.backendSupportsFork(String(backendField.currentValue))
                        text: "Fork"
                        checked: root.launchMode === "fork"
                        checkable: true
                        onClicked: root.setLaunchMode("fork")
                    }
                    Item {
                        Layout.fillWidth: true
                    }
                }

                ListView {
                    id: pastSessionList
                    visible: root.launchMode !== "fresh"
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? 132 : 0
                    model: root.controller.pastSessions
                    spacing: 4
                    clip: true

                    delegate: ItemDelegate {
                        id: pastRow
                        required property var modelData
                        width: ListView.view.width
                        height: 42
                        highlighted: root.pastSessionId === String(pastRow.modelData.id)
                        onClicked: root.pastSessionId = String(pastRow.modelData.id)
                        contentItem: Column {
                            spacing: 1
                            Text {
                                width: parent.width
                                text: String(pastRow.modelData.title || pastRow.modelData.preview || pastRow.modelData.id)
                                color: "#ddd5df"
                                elide: Text.ElideRight
                                font.pixelSize: 11
                            }
                            Text {
                                width: parent.width
                                text: String(pastRow.modelData.cwd || "")
                                color: "#746d7a"
                                elide: Text.ElideMiddle
                                font.pixelSize: 9
                            }
                        }
                    }

                    BusyIndicator {
                        anchors.centerIn: parent
                        visible: root.controller.pastSessionsLoading
                        running: visible
                    }
                }

                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: "#292b3a"
            }

            RowLayout {
                Layout.fillWidth: true
                Item {
                    Layout.fillWidth: true
                }
                Button {
                    text: "Cancel"
                    onClicked: root.closeRequested()
                }
                Button {
                    text: root.replaceSession.length > 0 ? "Relaunch" : "Start"
                    enabled: nameField.text.trim().length > 0 && workspaceField.text.trim().length > 0 && (root.launchMode === "fresh" || root.pastSessionId.length > 0)
                    onClicked: root.controller.createAgent(
                        nameField.text, workspaceField.text,
                        String(backendField.currentValue), String(modelField.currentValue),
                        String(effortField.currentValue), root.replaceSession,
                        root.launchMode, root.pastSessionId,
                        String(backendField.currentValue) === "claude"
                            ? root.selectedMcpServers : [])
                }
            }
        }
    }
}
