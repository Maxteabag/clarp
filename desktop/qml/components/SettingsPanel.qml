pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openConnection
    signal openOrchestrator
    signal closeRequested
    readonly property bool dialogOpen: ttsDialog.visible
    property int focusedIndex: 0
    readonly property var actions: [timestampsRow, toolsRow, narrationRow, spokenRow,
        connectionRow, orchestratorRow, filesystemRow, routingRow]
    color: "#171923"
    objectName: "settingsPanel"

    function availableRows() { return root.actions.filter(row => row.visible && row.enabled); }

    function focusRow(index) {
        if (!root.visible || root.dialogOpen) return;
        const available = root.availableRows();
        if (available.length === 0) return;
        const row = available[(index + available.length) % available.length];
        row.forceActiveFocus(Qt.TabFocusReason);
        Qt.callLater(() => root.revealRow(row));
    }

    function focusCurrent() {
        root.focusRow(Math.max(0, root.availableRows().indexOf(root.actions[root.focusedIndex])));
    }
    function dismissDialog() { ttsDialog.reject(); }

    function revealRow(row) {
        if (!root.visible || !row.activeFocus) return;
        const view = settingsScroll.contentItem as Flickable;
        if (!view) return;
        if (row === root.availableRows()[0]) {
            view.contentY = 0;
            return;
        }
        const top = row.mapToItem(settingsColumn, 0, 0).y;
        const bottom = top + row.height;
        const current = view.contentY;
        const target = top < current + 12 ? top - 12
            : bottom > current + view.height - 12 ? bottom - view.height + 12 : current;
        view.contentY = Math.max(0, Math.min(target, Math.max(0, view.contentHeight - view.height)));
    }

    function handleRowKey(event, row) {
        if (event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) return;
        const index = root.availableRows().indexOf(row);
        if (event.key === Qt.Key_Down || event.key === Qt.Key_J)
            root.focusRow(index + 1);
        else if (event.key === Qt.Key_Up || event.key === Qt.Key_K)
            root.focusRow(index - 1);
        else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab)
            root.focusRow(index + (event.key === Qt.Key_Backtab || (event.modifiers & Qt.ShiftModifier) ? -1 : 1));
        else if (event.key === Qt.Key_Home)
            root.focusRow(0);
        else if (event.key === Qt.Key_End)
            root.focusRow(-1);
        else if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (!event.isAutoRepeat) row.activated();
        } else if (row === narrationRow && (event.key === Qt.Key_Left || event.key === Qt.Key_Right)) {
            root.controller.toolNarrator.detailLevel = Math.max(0, Math.min(4,
                root.controller.toolNarrator.detailLevel + (event.key === Qt.Key_Right ? 1 : -1)));
        } else if (row.checkable && (event.key === Qt.Key_Left || event.key === Qt.Key_Right)) {
            if (row.checked !== (event.key === Qt.Key_Right)) row.activated();
        } else if (event.key === Qt.Key_Escape)
            root.closeRequested();
        else return;
        event.accepted = true;
    }

    function ttsChoices(includeNone) {
        const choices = includeNone ? [{ id: "none", label: "No fallback" }] : [];
        for (const provider of (controller.ttsProviderStatus.providers || [])) {
            if (provider.available !== false && provider.installed !== false)
                choices.push({ id: String(provider.id), label: String(provider.name || provider.label || provider.id) });
        }
        return choices;
    }

    ScrollView {
        id: settingsScroll
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            id: settingsColumn
            width: Math.min(760, Math.max(0, settingsScroll.availableWidth - 32))
            x: Math.max(16, (settingsScroll.availableWidth - width) / 2)
            spacing: 13

            Item { Layout.preferredHeight: 14 }
            Text {
                objectName: "settingsHeading"
                text: "SETTINGS"
                color: "#c9cde3"
                font.family: "JetBrains Mono"
                font.pixelSize: 20
                font.weight: Font.DemiBold
                font.letterSpacing: 1.6
            }
            Text {
                Layout.fillWidth: true
                text: "↑↓ / Tab move · Space / Enter change · Esc back to chat"
                color: "#858aa5"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            SettingsGroup {
                title: "CHATS"
                SettingsToggle {
                    id: timestampsRow
                    objectName: "setting-timestamps"
                    label: "Timestamps"
                    detail: "Show the date and time under each message"
                    checked: root.controller.timestampsVisible
                    onToggled: value => root.controller.timestampsVisible = value
                }
                SettingsToggle {
                    id: toolsRow
                    objectName: "setting-tools"
                    label: "Tool details"
                    detail: "Keep tool calls expanded in the timeline"
                    checked: root.controller.toolsVisible
                    onToggled: value => root.controller.toolsVisible = value
                }
            }

            SettingsGroup {
                title: "EXPERIMENTS"
                SettingsAction {
                    id: narrationRow
                    objectName: "setting-tool-narration"
                    label: "Tool detail"
                    detail: root.controller.toolNarrator.levelDescription
                    rowClickable: false
                    onActivated: root.controller.toolNarrator.detailLevel = (root.controller.toolNarrator.detailLevel + 1) % 5
                    contentItem: ColumnLayout {
                        spacing: 8
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "Tool detail"; color: "#d2d7eb"; font.pixelSize: 14 }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: root.controller.toolNarrator.detailLevels[root.controller.toolNarrator.detailLevel]
                                color: "#82aaff"
                                font.pixelSize: 14
                            }
                        }
                        Slider {
                            id: detailDial
                            objectName: "toolDetailDial"
                            Layout.fillWidth: true
                            from: 0
                            to: 4
                            stepSize: 1
                            snapMode: Slider.SnapAlways
                            live: true
                            focusPolicy: Qt.NoFocus
                            value: root.controller.toolNarrator.detailLevel
                            Accessible.name: "Tool detail, Developer to Grandma"
                            onMoved: root.controller.toolNarrator.detailLevel = Math.round(value)
                            onPressedChanged: { if (pressed) narrationRow.forceActiveFocus(Qt.MouseFocusReason); }
                            background: Rectangle {
                                x: detailDial.leftPadding
                                y: detailDial.topPadding + (detailDial.availableHeight - height) / 2
                                width: detailDial.availableWidth
                                height: 4
                                radius: 2
                                color: "#454c65"
                                Rectangle { width: parent.width * detailDial.visualPosition; height: parent.height; radius: 2; color: "#82aaff" }
                                Repeater {
                                    model: 5
                                    Rectangle {
                                        required property int index
                                        x: index * (parent.width - width) / 4
                                        y: -2
                                        width: 3
                                        height: 8
                                        color: index <= detailDial.value ? "#82aaff" : "#59627f"
                                    }
                                }
                            }
                            handle: Rectangle {
                                x: detailDial.leftPadding + detailDial.visualPosition * (detailDial.availableWidth - width)
                                y: detailDial.topPadding + (detailDial.availableHeight - height) / 2
                                implicitWidth: 16
                                implicitHeight: 16
                                radius: 8
                                color: "#c4cee9"
                                border.color: "#82aaff"
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "Developer"; color: "#858da8"; font.pixelSize: 11 }
                            Item { Layout.fillWidth: true }
                            Text { text: "← → to adjust"; color: "#858da8"; font.pixelSize: 11 }
                            Item { Layout.fillWidth: true }
                            Text { text: "Grandma"; color: "#858da8"; font.pixelSize: 11 }
                        }
                        Text {
                            Layout.fillWidth: true
                            text: narrationRow.detail
                            color: "#a2aac4"
                            font.pixelSize: 12
                            wrapMode: Text.Wrap
                        }
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: "Your Host uses Spark low to explain tool metadata and bounded script excerpts. Results are shared across clients; this detail choice stays on this device. Secret filtering is best-effort."
                    color: "#858aa5"
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    text: root.controller.toolNarrator.status
                    color: "#82aaff"
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
            }

            SettingsGroup {
                title: "VOICE & AUDIO"
                SettingsToggle {
                    id: spokenRow
                    objectName: "setting-spoken-replies"
                    label: "Spoken replies"
                    detail: "Mute or resume agent voice playback"
                    checked: !root.controller.muted
                    onToggled: value => root.controller.muted = !value
                }
            }

            SettingsGroup {
                title: "HOST"
                SettingsLink {
                    id: connectionRow
                    objectName: "setting-connection"
                    label: root.controller.serverName || "Clarp Host"
                    detail: root.controller.baseUrl + "  ·  " + root.controller.connectionState
                    onClicked: root.openConnection()
                }
                SettingsLink {
                    id: orchestratorRow
                    objectName: "setting-orchestrator"
                    label: "Orchestrator"
                    detail: "Routing policy for hands-free and delegated work"
                    onClicked: root.openOrchestrator()
                }
                SettingsToggle {
                    id: filesystemRow
                    objectName: "setting-shared-filesystem"
                    label: "Shared filesystem"
                    detail: "Use direct local paths only when this desktop and Host share files"
                    checked: root.controller.sharedFilesystem
                    onToggled: value => root.controller.sharedFilesystem = value
                }
            }

            SettingsGroup {
                title: "HOST STATUS"
                SettingsInfo {
                    label: "Diagnostics"
                    value: root.controller.settingsStatusLoading
                        ? "Checking…"
                        : root.controller.diagnosticsHealth.ready ? "Ready" : "Needs attention"
                }
                SettingsInfo {
                    label: "Speech to text"
                    value: root.controller.transcriptionCapabilities.available
                        ? "Available" : "Unavailable"
                }
                SettingsInfo {
                    label: "Transcription model"
                    value: String(root.controller.transcriptionCapabilities.default_model
                        || "Server default")
                }
                SettingsInfo {
                    label: "TTS queue"
                    value: String((root.controller.diagnosticsHealth.tts_queue || {}).pending || 0)
                        + " waiting  ·  "
                        + String((root.controller.diagnosticsHealth.tts_queue || {}).in_flight || 0)
                        + " active"
                }
                SettingsInfo {
                    label: "Voice provider"
                    value: String(root.controller.ttsProviderStatus.provider || "Unknown")
                        + (String(root.controller.ttsProviderStatus.fallback || "none") !== "none"
                            ? "  →  " + String(root.controller.ttsProviderStatus.fallback) : "")
                }
                SettingsLink {
                    id: routingRow
                    objectName: "setting-voice-routing"
                    label: "Voice routing"
                    detail: "Choose primary and fallback providers"
                    onClicked: {
                        const primary = root.ttsChoices(false);
                        const fallback = root.ttsChoices(true);
                        primaryProvider.model = primary;
                        fallbackProvider.model = fallback;
                        primaryProvider.currentIndex = Math.max(0,
                            primary.findIndex(item => item.id === String(root.controller.ttsProviderStatus.provider || "")));
                        fallbackProvider.currentIndex = Math.max(0,
                            fallback.findIndex(item => item.id === String(root.controller.ttsProviderStatus.fallback || "none")));
                        ttsDialog.open();
                    }
                }
            }

            SettingsGroup {
                title: "KEYBOARD"
                SettingsInfo { label: "Command palette"; value: "Ctrl+K" }
                SettingsInfo { label: "Settings"; value: "Ctrl+," }
                SettingsInfo { label: "Navigate settings"; value: "↑↓ / Tab · Home / End" }
                SettingsInfo { label: "Open native CLI"; value: "Ctrl+Alt+T" }
                SettingsInfo { label: "Start idle contact"; value: "Ctrl+Alt+N" }
                SettingsInfo { label: "Show / hide sidebar"; value: "Ctrl+B" }
                SettingsInfo { label: "Move between panes"; value: "Ctrl+Alt+Arrow" }
                SettingsInfo { label: "Split right / down"; value: "Ctrl+Alt+V / S" }
                SettingsInfo { label: "Zoom / close / balance"; value: "Ctrl+Alt+Z / X / =" }
                SettingsInfo { label: "Queue message"; value: "Ctrl+Enter" }
            }

            SettingsGroup {
                title: "ABOUT"
                SettingsInfo { label: "Desktop client"; value: "0.1.0 preview" }
                SettingsInfo {
                    label: "Host version"
                    value: root.controller.serverVersion || "Unknown"
                }
            }
            Item { Layout.preferredHeight: 24 }
        }
    }

    component SettingsGroup: Rectangle {
        id: group
        required property string title
        default property alias content: rows.data
        Layout.fillWidth: true
        implicitHeight: column.implicitHeight + 20
        radius: 6
        color: "transparent"
        border.width: 0
        ColumnLayout {
            id: column
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 10
            spacing: 8
            Text {
                text: group.title
                color: "#858aa5"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
                font.weight: Font.DemiBold
                font.letterSpacing: 1
            }
            ColumnLayout {
                id: rows
                Layout.fillWidth: true
                spacing: 4
            }
        }
    }

    component SettingsAction: Control {
        id: actionRow
        required property string label
        required property string detail
        property bool checkable: false
        property bool checked: false
        property bool rowClickable: true
        signal activated
        Layout.fillWidth: true
        implicitHeight: Math.max(52, contentItem.implicitHeight + 16)
        padding: 10
        focusPolicy: Qt.StrongFocus
        Accessible.role: checkable ? Accessible.CheckBox : Accessible.Button
        Accessible.name: label
        Accessible.description: detail
        Accessible.checkable: checkable
        Accessible.checked: checked
        Accessible.onPressAction: activated()
        Accessible.onToggleAction: { if (checkable) activated(); }

        background: Rectangle {
            radius: 4
            color: actionRow.activeFocus ? "#292d41" : hover.hovered ? "#212431" : "transparent"
            border.width: actionRow.activeFocus ? 1 : 0
            border.color: "#606888"
        }
        contentItem: RowLayout {
            spacing: 14
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text {
                    Layout.fillWidth: true
                    text: actionRow.label
                    color: actionRow.activeFocus ? "#e0e3f2" : "#c2c5d8"
                    font.pixelSize: 14
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    text: actionRow.detail
                    color: actionRow.activeFocus ? "#a2aac4" : "#777d95"
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
            Text {
                Layout.preferredWidth: 34
                horizontalAlignment: Text.AlignRight
                text: actionRow.checkable ? (actionRow.checked ? "ON" : "OFF") : "›"
                color: actionRow.checkable && actionRow.checked ? "#a6bf96" : "#9098b3"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }
        }
        HoverHandler { id: hover }
        TapHandler {
            enabled: actionRow.rowClickable
            onTapped: {
                actionRow.forceActiveFocus(Qt.MouseFocusReason);
                actionRow.activated();
            }
        }
        Keys.onPressed: event => root.handleRowKey(event, actionRow)
        onActiveFocusChanged: {
            if (activeFocus) {
                root.focusedIndex = root.actions.indexOf(actionRow);
                Qt.callLater(() => root.revealRow(actionRow));
            }
        }
    }

    component SettingsToggle: SettingsAction {
        checkable: true
        signal toggled(bool checked)
        onActivated: toggled(!checked)
    }

    component SettingsLink: SettingsAction {
        signal clicked
        onActivated: clicked()
    }

    component SettingsInfo: RowLayout {
        id: infoRow
        required property string label
        required property string value
        Layout.fillWidth: true
        Layout.leftMargin: 8
        Layout.rightMargin: 8
        Layout.preferredHeight: 28
        Text { text: infoRow.label; color: "#aeb2c8"; font.pixelSize: 12 }
        Item { Layout.fillWidth: true }
        Text {
            text: infoRow.value
            color: "#6d728a"
            font.family: "JetBrains Mono"
            font.pixelSize: 11
        }
    }

    Dialog {
        id: ttsDialog
        anchors.centerIn: parent
        modal: true
        focus: true
        title: "Voice routing"
        standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: primaryProvider.forceActiveFocus(Qt.TabFocusReason)
        onClosed: Qt.callLater(root.focusCurrent)
        onAccepted: root.controller.setTtsProviders(
            String(primaryProvider.currentValue || ""),
            String(fallbackProvider.currentValue || "none"), "")
        ColumnLayout {
            width: 360
            Label { text: "Primary provider" }
            ComboBox {
                id: primaryProvider
                objectName: "settingsPrimaryProvider"
                Layout.fillWidth: true
                textRole: "label"
                valueRole: "id"
            }
            Label { text: "Fallback" }
            ComboBox {
                id: fallbackProvider
                Layout.fillWidth: true
                textRole: "label"
                valueRole: "id"
            }
            Text {
                Layout.fillWidth: true
                text: "Changing to a local provider may briefly restart the Host."
                color: "#6d728a"
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
        }
    }

    onVisibleChanged: {
        if (visible) {
            controller.loadSettingsStatus();
            Qt.callLater(root.focusCurrent);
        }
    }
    Component.onCompleted: { if (visible) Qt.callLater(root.focusCurrent); }
}
