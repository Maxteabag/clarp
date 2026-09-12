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
    property bool previewVersionsAvailable: false
    readonly property bool narrationEnabled: controller.toolNarrator !== undefined
        && controller.toolNarrator !== null && controller.toolNarrator.enabled
    signal commandRequested(string action)
    signal agentRequested(string session)
    signal contactRequested(string name)
    function settingToggle(property, label, keywords) {
        const enabled = Boolean(root.controller[property]);
        return {kind: "command", action: "setting:" + property,
            label: (enabled ? "On → Off · " : "Off → On · ") + label,
            key: property === "toolsVisible" ? "Ctrl+Shift+T" : "", group: "settings", keywords: keywords};
    }
    readonly property var settingCommands: {
        const rows = [
            settingToggle("minimalUi", "Minimal UI", "hide sidebar chevron button minimalistic"),
            settingToggle("timestampsVisible", "Timestamps", "date time messages"),
            settingToggle("showWhenReady", "Show when ready", "stream streaming answers typing"),
            settingToggle("pauseMobilePush", "Pause phone alerts on desktop", "push notifications mobile iphone"),
            settingToggle("sharedFilesystem", "Shared filesystem access (trusted Host)", "files local folders")
        ];
        ["Grouped", "Always visible", "Group old"].forEach((label, mode) => {
            rows.push({kind: "command", action: "setting:activity:" + mode,
                label: "Tool activity: " + label + (root.controller.activityDisplayMode === mode ? " (current)" : ""),
                key: "", group: "settings", keywords: "tool calls collapse expand grouping"});
        });
        const narrator = root.controller.toolNarrator;
        if (narrator) {
            for (let level = 0; level < narrator.detailLevels.length; ++level) {
                rows.push({kind: "command", action: "setting:detail:" + level,
                    label: "Tool detail: " + narrator.detailLevels[level]
                        + (narrator.detailLevel === level ? " (current)" : level > 0 ? " (uses AI)" : " (no AI)"),
                    key: "", group: "settings", keywords: "explanation explanations narration audience"});
            }
        }
        return rows;
    }
    function applySetting(action) {
        if (action.startsWith("setting:activity:")) {
            const mode = Number(action.slice("setting:activity:".length));
            if (Number.isInteger(mode) && mode >= 0 && mode <= 2) root.controller.activityDisplayMode = mode;
        } else if (action.startsWith("setting:detail:")) {
            const level = Number(action.slice("setting:detail:".length));
            const narrator = root.controller.toolNarrator;
            if (narrator && Number.isInteger(level) && level >= 0 && level < narrator.detailLevels.length)
                narrator.detailLevel = level;
        } else {
            const property = action.slice("setting:".length);
            if (["minimalUi", "timestampsVisible", "showWhenReady", "toolsVisible", "pauseMobilePush", "sharedFilesystem"].includes(property))
                root.controller[property] = !root.controller[property];
        }
    }
    readonly property var commands: [
        { kind: "command", label: "New contact & chat", action: "quick-new-agent", key: "Ctrl+Shift+N", group: "agent" },
        { kind: "command", label: "Rename contact", action: "rename-agent", key: "F2", group: "agent", keywords: "rename name title relabel persona" },
        { kind: "command", label: "Preview versions · update or roll back", action: "preview-versions", key: "", group: "settings", keywords: "previous installs rollback downgrade" },
        { kind: "command", label: "New agent", action: "new", key: "Ctrl+N", group: "agent" },
        { kind: "command", label: "Start an idle contact", action: "new-contact", key: "Ctrl+Alt+N", group: "agent" },
        { kind: "command", label: "Open agent in terminal", action: "agent-terminal", key: "Ctrl+Alt+T", group: "agent" },
        { kind: "command", label: root.narrationEnabled ? "Disable plain-English tools" : "Enable plain-English tools (Spark · extra usage)", action: "tool-narration", key: "", group: "experiment" },
        { kind: "command", label: "Split right", action: "split-right", key: "Ctrl+Alt+V", group: "layout" },
        { kind: "command", label: "Split down", action: "split-down", key: "Ctrl+Alt+S", group: "layout" },
        { kind: "command", label: "Close pane", action: "close-pane", key: "Ctrl+Alt+X", group: "layout" },
        { kind: "command", label: "Zoom pane", action: "zoom", key: "Ctrl+Alt+Z", group: "layout" },
        { kind: "command", label: "Balance panes", action: "balance", key: "Ctrl+Alt+=", group: "layout" },
        { kind: "command", label: root.sidebarVisible ? "Hide sidebar" : "Show sidebar", action: "sidebar", key: "Ctrl+B", group: "view" },
        { kind: "command", label: "Show/hide keybindings", action: "shortcut-bar", key: "Ctrl+Shift+K", group: "view" },
        { kind: "command", label: "Larger interface", action: "ui-larger", key: "Ctrl+=", group: "view" },
        { kind: "command", label: "Smaller interface", action: "ui-smaller", key: "Ctrl+-", group: "view" },
        { kind: "command", label: "Reset interface size", action: "ui-reset", key: "Ctrl+0", group: "view" },
        { kind: "command", label: "Update desktop preview", action: "update-preview", key: "Ctrl+Alt+U", group: "view", keywords: "upgrade restart version" },
        { kind: "command", label: "Jump to latest", action: "jump-latest", key: "Ctrl+End", group: "view", keywords: "bottom newest scroll follow" },
        { kind: "command", label: "Retry latest failed message", action: "retry-message", key: "Ctrl+Alt+R", group: "view", keywords: "resend send delivery not delivered" },
        { kind: "command", label: "Dismiss conversation error", action: "dismiss-error", key: "Esc", group: "view", keywords: "clear close error warning banner voice synthesis failed" },
        { kind: "command", label: "Change directory", action: "change-directory", key: "Ctrl+Alt+D", group: "agent", keywords: "folder workspace cwd new chat" },
        { kind: "command", label: "Refresh conversation", action: "refresh", key: "Ctrl+R", group: "view" },
        { kind: "command", label: "Agent overview", action: "overview", key: "Ctrl+Shift+O", group: "view" },
        { kind: "command", label: "Chats", action: "chats", key: "Ctrl+1", group: "destination" },
        { kind: "command", label: "Updates", action: "updates", key: "Ctrl+2", group: "destination" },
        { kind: "command", label: "Teams", action: "teams", key: "Ctrl+3", group: "destination" },
        { kind: "command", label: "Settings", action: "settings", key: "Ctrl+,", group: "destination" },
        { kind: "command", label: "Host connection", action: "connection", key: "", group: "settings" },
        { kind: "command", label: "Orchestrator settings", action: "orchestrator", key: "", group: "view" },
        { kind: "command", label: "Next agent needing attention", action: "next-attention", key: "Ctrl+J", group: "agent" },
        { kind: "command", label: "Release agent", action: "release-agent", key: "Ctrl+Shift+R", group: "agent" },
        { kind: "command", label: "Stop agent", action: "stop-agent", key: "Ctrl+.", group: "agent" },
        { kind: "command", label: root.controller.muted ? "Enable voice replies" : "Mute voice replies", action: "mute", key: "Ctrl+M", group: "settings" },
        { kind: "command", label: "Talk", action: "talk", key: "Ctrl+Shift+Space", group: "audio" }
    ].filter(command => command.action !== "preview-versions" || root.previewVersionsAvailable)
    readonly property var results: {
        controller.agentRevision;
        controller.contacts.count;
        controller.lastBackend;
        controller.selectedSession;
        const needle = query.trim().toLowerCase();
        const terms = needle.split(/\s+/).filter(term => term.length > 0);
        const commandRows = root.commands.concat(root.settingCommands).filter(command => {
            const searchable = (command.label + " " + command.group + " " + command.key
                + " " + (command.keywords || "")).toLowerCase();
            return terms.every(term => searchable.includes(term));
        });
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
            directory: "~"
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
            if (String(item.action).startsWith("setting:")) root.applySetting(String(item.action));
            else root.commandRequested(String(item.action));
            if (["preview-versions", "quick-new-agent", "rename-agent", "new", "overview", "connection", "orchestrator", "updates", "teams", "settings"].includes(String(item.action)))
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
        radius: 0
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

            TuiTextField {
                id: search
                Layout.fillWidth: true
                text: root.query
                Layout.preferredHeight: 43
                placeholderText: root.contactsOnly ? "Start an idle contact" : "Agent, contact, setting or command"
                font.family: "JetBrains Mono"
                font.pixelSize: 14
                leftPadding: 12
                rightPadding: 12
                background: Rectangle {
                    color: "#1a1b26"
                    border.color: "#303246"
                    radius: 0
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
                        radius: 0
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
                            TuiText {
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
                            TuiText {
                                text: String(resultRow.modelData.kind) === "command"
                                    ? String(resultRow.modelData.label)
                                    : (String(resultRow.modelData.kind) === "contact" ? "Start " : "")
                                        + String(resultRow.modelData.name)
                                color: "#c6c8dc"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 12
                                font.weight: Font.Medium
                            }
                            TuiText {
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
                        TuiText {
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
                            radius: 0
                            color: "#20212d"
                            border.color: "#36384b"
                            TuiText {
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

            TuiText {
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
