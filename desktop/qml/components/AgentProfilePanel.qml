pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string session: ""
    readonly property int agentRevision: controller.agentRevision
    readonly property var details: {
        agentRevision;
        return controller.agentDetails(session);
    }
    signal closeRequested
    signal queueRequested(string session)
    signal voiceRequested(string session, string name)
    signal relaunchRequested(string session, string name)
    color: "#c008090f"

    function indexOfValue(rows, value) {
        for (let i = 0; i < rows.length; ++i) {
            const candidate = typeof rows[i] === "string" ? rows[i] : String(rows[i].id || "");
            if (candidate === value)
                return i;
        }
        return 0;
    }

    function setMcpEnabled(name, enabled) {
        const selected = Array.from(root.details.mcp_servers || []);
        const index = selected.indexOf(name);
        if (enabled && index < 0)
            selected.push(name);
        else if (!enabled && index >= 0)
            selected.splice(index, 1);
        root.controller.setAgentMcp(root.session, selected);
    }

    MouseArea { anchors.fill: parent; onClicked: root.closeRequested() }

    Rectangle {
        width: Math.min(820, parent.width - 36)
        height: Math.min(760, parent.height - 42)
        anchors.centerIn: parent
        radius: 8
        color: "#171923"
        border.color: "#41465f"

        MouseArea { anchors.fill: parent; onClicked: mouse => mouse.accepted = true }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 58
                Layout.leftMargin: 16
                Layout.rightMargin: 10
                spacing: 11
                AgentAvatar {
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                    controller: root.controller
                    session: root.session
                    name: String(root.details.name || "Agent")
                    avatarSize: 34
                    cornerRadius: 8
                    fallbackColor: "#4b5068"
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: String(root.details.name || "Agent")
                        color: "#d0d3e4"
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(root.details.backend || "") + "  ·  " + root.session
                        color: "#686d84"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 8
                        elide: Text.ElideRight
                    }
                }
                StatusPill { status: String(root.details.state || "offline") }
                ToolButton { text: "×"; onClicked: root.closeRequested() }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#303347" }

            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                ColumnLayout {
                    width: Math.max(420, parent.width - 28)
                    x: 14
                    spacing: 11

                    Item { Layout.preferredHeight: 3 }

                    ProfileCard {
                        title: "THIS CHAT"
                        ProfileInfo { label: "Session"; value: root.session }
                        ProfileInfo {
                            label: "Folder"
                            value: String(root.details.working_directory || "Unavailable")
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Button {
                                text: "Open files"
                                onClicked: root.controller.openAgentFiles(root.session)
                            }
                            Button {
                                text: "Open terminal"
                                onClicked: root.controller.openAgentTerminal(root.session)
                            }
                            Button {
                                text: "Voice"
                                onClicked: root.voiceRequested(
                                    root.session, String(root.details.name || "Agent"))
                            }
                            Item { Layout.fillWidth: true }
                        }
                    }

                    ProfileCard {
                        visible: root.controller.profileLoading
                            || Object.keys(root.controller.profileTaskPlan).length > 0
                        Layout.preferredHeight: visible ? implicitHeight : 0
                        title: "CURRENT PLAN"
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                text: String(root.controller.profileTaskPlan.title || "Loading plan…")
                                color: "#c2c6d9"
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }
                            Text {
                                visible: !root.controller.profileLoading
                                text: String(root.controller.profileTaskPlan.completed_count || 0)
                                    + "/" + String(root.controller.profileTaskPlan.total_count || 0)
                                color: "#7f859f"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 9
                            }
                            BusyIndicator {
                                visible: root.controller.profileLoading
                                running: visible
                                implicitWidth: 18
                                implicitHeight: 18
                            }
                        }
                        Repeater {
                            model: root.controller.profileTaskPlan.items || []
                            delegate: RowLayout {
                                id: taskRow
                                required property var modelData
                                Layout.fillWidth: true
                                Rectangle {
                                    Layout.preferredWidth: 7
                                    Layout.preferredHeight: 7
                                    radius: 3.5
                                    color: String(taskRow.modelData.status || "") === "completed"
                                        ? "#8da77f"
                                        : String(taskRow.modelData.status || "") === "in_progress"
                                            ? "#a8aed7" : "#51566b"
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: String(taskRow.modelData.title || "Task")
                                    color: "#adb1c7"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Text {
                                    visible: (taskRow.modelData.subtasks || []).length > 0
                                    text: String((taskRow.modelData.subtasks || []).length) + " steps"
                                    color: "#62677e"
                                    font.pixelSize: 8
                                }
                            }
                        }
                    }

                    ProfileCard {
                        id: llmCard
                        title: "MODEL & EFFORT"
                        readonly property var modelRows: root.controller.modelsForBackend(
                                String(root.details.backend || ""))
                        readonly property var effortRows: root.controller.effortsForModel(
                                String(root.details.backend || ""),
                                String(modelBox.currentValue || ""))
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "Model"; color: "#9297af"; font.pixelSize: 10 }
                            ComboBox {
                                id: modelBox
                                Layout.fillWidth: true
                                model: llmCard.modelRows
                                textRole: "label"
                                valueRole: "id"
                                currentIndex: root.indexOfValue(
                                    llmCard.modelRows,
                                    String(root.details.model || ""))
                                onActivated: {
                                    const chosen = String(currentValue || "");
                                    const options = root.controller.effortsForModel(
                                        String(root.details.backend || ""), chosen);
                                    let effort = String(root.details.effort || "");
                                    if (root.indexOfValue(options, effort) === 0 && effort.length > 0)
                                        effort = "";
                                    root.controller.setAgentLlm(root.session, chosen, effort);
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "Effort"; color: "#9297af"; font.pixelSize: 10 }
                            ComboBox {
                                id: effortBox
                                Layout.fillWidth: true
                                model: llmCard.effortRows
                                textRole: "label"
                                valueRole: "id"
                                currentIndex: root.indexOfValue(
                                    llmCard.effortRows,
                                    String(root.details.effort || ""))
                                onActivated: root.controller.setAgentLlm(
                                    root.session, String(modelBox.currentValue || ""),
                                    String(currentValue || ""))
                            }
                        }
                    }

                    ProfileCard {
                        title: "AUTONOMY & NOTIFICATIONS"
                        ProfileToggle {
                            label: "Heartbeat"
                            checked: Boolean(root.details.heartbeat_enabled)
                            onToggled: root.controller.setAgentHeartbeat(root.session, checked)
                        }
                        ProfileToggle {
                            label: "Dreaming"
                            checked: Boolean(root.details.dreaming_enabled)
                            onToggled: root.controller.setAgentDreaming(root.session, checked)
                        }
                        ProfileToggle {
                            label: "Push alerts"
                            checked: !Boolean(root.details.muted)
                            onToggled: root.controller.setAgentPushMuted(root.session, !checked)
                        }
                    }

                    ProfileCard {
                        visible: String(root.details.backend || "") === "claude"
                            && root.controller.availableMcpServers.length > 0
                        Layout.preferredHeight: visible ? implicitHeight : 0
                        title: "MCP SERVERS"
                        Repeater {
                            model: root.controller.availableMcpServers
                            delegate: CheckBox {
                                id: mcpChoice
                                required property string modelData
                                Layout.fillWidth: true
                                text: modelData
                                checked: Array.from(root.details.mcp_servers || [])
                                    .includes(modelData)
                                onToggled: root.setMcpEnabled(modelData, checked)
                            }
                        }
                    }

                    ProfileCard {
                        title: "CONTEXT"
                        ProgressBar {
                            Layout.fillWidth: true
                            from: 0
                            to: Math.max(1, Number(root.details.context_window || 1))
                            value: Number(root.details.context_tokens || 0)
                        }
                        ProfileInfo {
                            label: "Used"
                            value: String(root.details.context_tokens || 0) + " / "
                                + String(root.details.context_window || "unknown") + " tokens"
                        }
                        Button {
                            text: "Compact context"
                            enabled: String(root.details.state || "") !== "compacting"
                            onClicked: compactDialog.open()
                        }
                    }

                    ProfileCard {
                        title: "QUEUED MESSAGES"
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                text: Number(root.details.queue_count || 0) === 0
                                    ? "Nothing waiting" : String(root.details.queue_count) + " waiting"
                                color: "#858aa2"
                                font.pixelSize: 10
                            }
                            Button {
                                text: "Manage"
                                onClicked: root.queueRequested(root.session)
                            }
                        }
                    }

                    ProfileCard {
                        visible: (root.details.schedules || []).length > 0
                        Layout.preferredHeight: visible ? implicitHeight : 0
                        title: "SCHEDULED TASKS"
                        Repeater {
                            model: root.details.schedules || []
                            delegate: RowLayout {
                                id: schedule
                                required property var modelData
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: String(schedule.modelData.name || "Scheduled task")
                                    color: "#adb1c7"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Switch {
                                    checked: Boolean(schedule.modelData.enabled)
                                    onToggled: root.controller.setScheduleEnabled(
                                        String(schedule.modelData.schedule_id || ""), checked)
                                }
                            }
                        }
                    }

                    ProfileCard {
                        title: "CHAT ACTIONS"
                        RowLayout {
                            Layout.fillWidth: true
                            Button {
                                text: "Relaunch"
                                onClicked: root.relaunchRequested(
                                    root.session, String(root.details.name || "Agent"))
                            }
                            Item { Layout.fillWidth: true }
                            Button {
                                visible: String(root.details.name || "").toLowerCase() !== "mike"
                                text: "Release…"
                                onClicked: releaseDialog.open()
                            }
                        }
                    }
                    Item { Layout.preferredHeight: 8 }
                }
            }
        }
    }

    Dialog {
        id: compactDialog
        anchors.centerIn: parent
        modal: true
        title: "Compact this conversation?"
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: root.controller.compactSession(root.session)
        Label { text: "Older context is summarized while the session stays open." }
    }

    Dialog {
        id: releaseDialog
        anchors.centerIn: parent
        modal: true
        title: "Release this chat?"
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: {
            root.controller.releaseAgent(root.session);
            root.closeRequested();
        }
        Label { text: "The contact, history, files, and artifacts remain." }
    }

    component ProfileCard: Rectangle {
        id: card
        required property string title
        default property alias content: cardRows.data
        Layout.fillWidth: true
        implicitHeight: cardColumn.implicitHeight + 20
        radius: 6
        color: "#1d202c"
        border.color: "#303448"
        ColumnLayout {
            id: cardColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 10
            spacing: 8
            Text {
                text: card.title
                color: "#858aa5"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
                font.weight: Font.DemiBold
                font.letterSpacing: 1
            }
            ColumnLayout { id: cardRows; Layout.fillWidth: true; spacing: 6 }
        }
    }

    component ProfileInfo: RowLayout {
        id: info
        required property string label
        required property string value
        Layout.fillWidth: true
        Text { text: info.label; color: "#888da5"; font.pixelSize: 9 }
        Item { Layout.fillWidth: true }
        Text {
            Layout.maximumWidth: 520
            text: info.value
            color: "#b2b6cc"
            font.family: "JetBrains Mono"
            font.pixelSize: 9
            elide: Text.ElideMiddle
        }
    }

    component ProfileToggle: RowLayout {
        id: toggle
        required property string label
        property bool checked: false
        signal toggled(bool checked)
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: toggle.label; color: "#adb1c7"; font.pixelSize: 10 }
        Switch { checked: toggle.checked; onToggled: toggle.toggled(checked) }
    }
}
