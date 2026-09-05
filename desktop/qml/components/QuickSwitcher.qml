pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string query: ""
    property bool restoreComposer: false
    property bool sidebarVisible: true
    property bool contactsOnly: false
    readonly property bool narrationEnabled: controller.toolNarrator !== undefined
        && controller.toolNarrator !== null && controller.toolNarrator.enabled
    signal commandRequested(string action)
    signal agentRequested(string session)
    signal contactRequested(string name)
    readonly property var commands: [
        { kind: "command", label: "New agent", action: "new", key: "Ctrl+N", group: "agent" },
        { kind: "command", label: "Start an idle contact", action: "new-contact", key: "Ctrl+Alt+N", group: "agent" },
        { kind: "command", label: "Open agent in terminal", action: "agent-terminal", key: "Ctrl+Alt+T", group: "agent" },
        { kind: "command", label: root.narrationEnabled ? "Disable plain-English tools" : "Enable plain-English tools (Spark · extra usage)", action: "tool-narration", key: "", group: "experiment" },
        { kind: "command", label: "Split right", action: "split-right", key: "Ctrl+Alt+V", group: "layout" },
        { kind: "command", label: "Split down", action: "split-down", key: "Ctrl+Alt+S", group: "layout" },
        { kind: "command", label: "Close pane", action: "close-pane", key: "Ctrl+Alt+X", group: "layout" },
        { kind: "command", label: "Zoom pane", action: "zoom", key: "Ctrl+Alt+Z", group: "layout" },
        { kind: "command", label: "Balance panes", action: "balance", key: "Ctrl+Alt+=", group: "layout" },
        { kind: "command", label: "Show or hide tools", action: "tools", key: "Ctrl+Shift+T", group: "view" },
        { kind: "command", label: root.sidebarVisible ? "Hide sidebar" : "Show sidebar", action: "sidebar", key: "Ctrl+B", group: "view" },
        { kind: "command", label: "Larger interface", action: "ui-larger", key: "Ctrl+=", group: "view" },
        { kind: "command", label: "Smaller interface", action: "ui-smaller", key: "Ctrl+-", group: "view" },
        { kind: "command", label: "Reset interface size", action: "ui-reset", key: "Ctrl+0", group: "view" },
        { kind: "command", label: "Refresh conversation", action: "refresh", key: "Ctrl+R", group: "view" },
        { kind: "command", label: "Agent overview", action: "overview", key: "Ctrl+Shift+O", group: "view" },
        { kind: "command", label: "Chats", action: "chats", key: "Ctrl+1", group: "destination" },
        { kind: "command", label: "Updates", action: "updates", key: "Ctrl+2", group: "destination" },
        { kind: "command", label: "Teams", action: "teams", key: "Ctrl+3", group: "destination" },
        { kind: "command", label: "Settings", action: "settings", key: "Ctrl+,", group: "destination" },
        { kind: "command", label: "Host connection", action: "connection", key: "", group: "settings" },
        { kind: "command", label: "Orchestrator settings", action: "orchestrator", key: "", group: "view" },
        { kind: "command", label: "Stop agent", action: "stop-agent", key: "Ctrl+.", group: "agent" },
        { kind: "command", label: "Toggle voice replies", action: "mute", key: "Ctrl+M", group: "audio" },
        { kind: "command", label: "Talk", action: "talk", key: "Ctrl+Shift+Space", group: "audio" }
    ]
    readonly property var results: {
        controller.agentRevision;
        controller.contacts.count;
        controller.lastBackend;
        controller.selectedSession;
        const needle = query.trim().toLowerCase();
        const commandRows = root.commands.filter(command => needle.length === 0
            || (command.label + " " + command.group + " " + command.key).toLowerCase().includes(needle));
        const agentRows = controller.matchingAgents(query).map(agent => ({
            kind: "agent",
            session: String(agent.session),
            name: String(agent.name),
            backend: String(agent.backend),
            state: String(agent.state),
            busy: Boolean(agent.busy),
            unread: Boolean(agent.unread)
        }));
        const contactRows = controller.matchingContacts(query).map(contact => ({
            kind: "contact", name: String(contact.name),
            backend: controller.quickStartBackend(),
            directory: controller.lastWorkingDirectory || "~"
        }));
        if (root.contactsOnly) return contactRows;
        return needle.length > 0 ? agentRows.concat(contactRows, commandRows)
            : commandRows.concat(agentRows, contactRows);
    }
    color: "#aa08090f"

    function open(returnToComposer) {
        contactsOnly = false;
        restoreComposer = Boolean(returnToComposer);
        query = "";
        visible = true;
        search.forceActiveFocus();
    }

    function openContacts(returnToComposer) {
        open(returnToComposer);
        contactsOnly = true;
        resultList.currentIndex = root.results.length > 0 ? 0 : -1;
    }

    function close(restoreFocus) {
        const shouldRestore = restoreFocus === undefined
            ? root.restoreComposer : Boolean(restoreFocus);
        visible = false;
        if (shouldRestore)
            Qt.callLater(() => controller.requestComposerFocus(controller.panes.activePaneId));
    }

    function choose(index) {
        if (index < 0 || index >= results.length)
            return;
        const item = results[index];
        let shouldRestore = root.restoreComposer;
        if (String(item.kind) === "command") {
            if (String(item.action) === "new-contact") {
                root.openContacts(root.restoreComposer);
                return;
            }
            root.commandRequested(String(item.action));
            if (["new", "overview", "connection", "orchestrator", "updates", "teams", "settings"].includes(String(item.action)))
                shouldRestore = false;
        } else if (String(item.kind) === "contact") {
            root.contactRequested(String(item.name));
            shouldRestore = true;
        } else {
            root.agentRequested(String(item.session));
        }
        root.close(shouldRestore);
    }

    MouseArea {
        anchors.fill: parent
        onClicked: root.close()
    }

    Rectangle {
        width: Math.min(560, parent.width - 32)
        height: Math.min(500, parent.height - 80)
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: Math.min(145, parent.height * 0.15)
        radius: 6
        color: "#1a1b26"
        border.color: "#3c3f58"

        MouseArea {
            anchors.fill: parent
            onClicked: mouse => mouse.accepted = true
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 5
            spacing: 4

            TextField {
                id: search
                Layout.fillWidth: true
                text: root.query
                Layout.preferredHeight: 43
                placeholderText: root.contactsOnly ? "Start an idle contact" : "Agent, contact or command"
                font.family: "JetBrains Mono"
                font.pixelSize: 14
                leftPadding: 12
                rightPadding: 12
                background: Rectangle {
                    color: "#1a1b26"
                    border.color: "#303246"
                    radius: 3
                }
                onTextChanged: {
                    root.query = text;
                    resultList.currentIndex = root.results.length > 0 ? 0 : -1;
                }
                Keys.onPressed: event => {
                    if (event.key === Qt.Key_Down) {
                        resultList.currentIndex = Math.min(root.results.length - 1, resultList.currentIndex + 1);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Up) {
                        resultList.currentIndex = Math.max(0, resultList.currentIndex - 1);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                        root.choose(resultList.currentIndex);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Escape) {
                        root.close();
                        event.accepted = true;
                    }
                }
            }

            ListView {
                id: resultList
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: root.results
                spacing: 1
                clip: true
                currentIndex: root.results.length > 0 ? 0 : -1

                delegate: ItemDelegate {
                    id: resultRow
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: String(modelData.kind) === "command" ? 36 : 50
                    highlighted: ListView.isCurrentItem
                    hoverEnabled: true
                    onHoveredChanged: {
                        if (hovered)
                            resultList.currentIndex = index;
                    }
                    onClicked: root.choose(index)

                    background: Rectangle {
                        radius: 3
                        color: resultRow.highlighted ? "#2a2c3c" : resultRow.hovered ? "#22232f" : "transparent"

                        Rectangle {
                            visible: resultRow.highlighted
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: 2
                            height: parent.height - 10
                            color: "#9da1bd"
                        }
                    }
                    contentItem: RowLayout {
                        spacing: 9
                        Item {
                            Layout.preferredWidth: 24
                            Layout.preferredHeight: 24

                            AgentAvatar {
                                visible: String(resultRow.modelData.kind) === "agent"
                                anchors.fill: parent
                                controller: root.controller
                                session: String(resultRow.modelData.session || "")
                                name: String(resultRow.modelData.name || "")
                                avatarSize: 24
                                cornerRadius: 6
                                fallbackColor: "#414458"
                            }
                            Text {
                                visible: String(resultRow.modelData.kind) !== "agent"
                                anchors.centerIn: parent
                                text: String(resultRow.modelData.kind) === "contact" ? "+" : "›"
                                color: "#8589a5"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 15
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                text: String(resultRow.modelData.kind) === "command"
                                    ? String(resultRow.modelData.label)
                                    : (String(resultRow.modelData.kind) === "contact" ? "Start " : "")
                                        + String(resultRow.modelData.name)
                                color: "#c6c8dc"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 12
                                font.weight: Font.Medium
                            }
                            Text {
                                visible: String(resultRow.modelData.kind) !== "command"
                                text: String(resultRow.modelData.kind) === "contact"
                                    ? "New session · " + String(resultRow.modelData.backend) + " · " + String(resultRow.modelData.directory)
                                    : String(resultRow.modelData.backend) + " · " + String(resultRow.modelData.session)
                                Layout.fillWidth: true
                                elide: Text.ElideMiddle
                                color: "#62657b"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                        }
                        StatusPill {
                            status: String(resultRow.modelData.kind) === "agent"
                                ? String(resultRow.modelData.state) : "idle"
                        }
                        Text {
                            visible: String(resultRow.modelData.kind) === "command"
                            text: String(resultRow.modelData.group || "").toUpperCase()
                            color: "#55586e"
                            font.family: "JetBrains Mono"
                            font.pixelSize: 11
                            font.letterSpacing: 0.6
                        }
                        Rectangle {
                            visible: String(resultRow.modelData.kind) === "command"
                                && String(resultRow.modelData.key || "").length > 0
                            Layout.preferredWidth: visible ? shortcutText.implicitWidth + 10 : 0
                            Layout.preferredHeight: 19
                            radius: 3
                            color: "#20212d"
                            border.color: "#36384b"
                            Text {
                                id: shortcutText
                                anchors.centerIn: parent
                                text: String(resultRow.modelData.key || "")
                                color: "#9a9db8"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                        }
                    }
                }
            }

            Text {
                visible: root.results.length === 0
                Layout.alignment: Qt.AlignHCenter
                text: root.contactsOnly ? "No idle contacts" : "No matching agent or contact"
                color: "#62657b"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }
        }
    }
}
