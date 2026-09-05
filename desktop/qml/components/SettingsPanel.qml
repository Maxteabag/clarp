pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openConnection
    signal openOrchestrator
    color: "#171923"
    objectName: "settingsPanel"

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
            width: Math.min(760, Math.max(420, settingsScroll.availableWidth - 48))
            x: Math.max(24, (settingsScroll.availableWidth - width) / 2)
            spacing: 13

            Item { Layout.preferredHeight: 14 }
            Text {
                text: "SETTINGS"
                color: "#c9cde3"
                font.family: "JetBrains Mono"
                font.pixelSize: 20
                font.weight: Font.DemiBold
                font.letterSpacing: 1.6
            }
            Text {
                text: "Desktop preferences and this Host"
                color: "#676c84"
                font.pixelSize: 12
            }

            SettingsGroup {
                title: "CHATS"
                SettingsToggle {
                    label: "Timestamps"
                    detail: "Show the date and time under each message"
                    checked: root.controller.timestampsVisible
                    onToggled: root.controller.timestampsVisible = checked
                }
                SettingsToggle {
                    label: "Tool details"
                    detail: "Keep tool calls expanded in the timeline"
                    checked: root.controller.toolsVisible
                    onToggled: root.controller.toolsVisible = checked
                }
            }

            SettingsGroup {
                title: "VOICE & AUDIO"
                SettingsToggle {
                    label: "Spoken replies"
                    detail: "Mute or resume agent voice playback"
                    checked: !root.controller.muted
                    onToggled: root.controller.muted = !checked
                }
            }

            SettingsGroup {
                title: "HOST"
                SettingsLink {
                    label: root.controller.serverName || "Clarp Host"
                    detail: root.controller.baseUrl + "  ·  " + root.controller.connectionState
                    onClicked: root.openConnection()
                }
                SettingsLink {
                    label: "Orchestrator"
                    detail: "Routing policy for hands-free and delegated work"
                    onClicked: root.openOrchestrator()
                }
                SettingsToggle {
                    label: "Shared filesystem"
                    detail: "Use direct local paths only when this desktop and Host share files"
                    checked: root.controller.sharedFilesystem
                    onToggled: root.controller.sharedFilesystem = checked
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

    component SettingsToggle: Rectangle {
        id: toggleRow
        required property string label
        required property string detail
        property bool checked: false
        signal toggled(bool checked)
        Layout.fillWidth: true
        implicitHeight: 48
        radius: 4
        color: "transparent"
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 10
            anchors.rightMargin: 8
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text { text: toggleRow.label; color: "#c2c5d8"; font.pixelSize: 13 }
                Text { text: toggleRow.detail; color: "#666b82"; font.pixelSize: 11 }
            }
            Switch {
                checked: toggleRow.checked
                onToggled: toggleRow.toggled(checked)
            }
        }
    }

    component SettingsLink: Rectangle {
        id: linkRow
        required property string label
        required property string detail
        signal clicked
        Layout.fillWidth: true
        implicitHeight: 48
        radius: 4
        color: tap.hovered ? "#252839" : "transparent"
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 10
            anchors.rightMargin: 10
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text { text: linkRow.label; color: "#c2c5d8"; font.pixelSize: 13 }
                Text {
                    Layout.fillWidth: true
                    text: linkRow.detail
                    color: "#666b82"
                    font.pixelSize: 11
                    elide: Text.ElideMiddle
                }
            }
            Text { text: "›"; color: "#737991"; font.pixelSize: 17 }
        }
        HoverHandler { id: tap }
        TapHandler { onTapped: linkRow.clicked() }
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
        title: "Voice routing"
        standardButtons: Dialog.Save | Dialog.Cancel
        onAccepted: root.controller.setTtsProviders(
            String(primaryProvider.currentValue || ""),
            String(fallbackProvider.currentValue || "none"), "")
        ColumnLayout {
            width: 360
            Label { text: "Primary provider" }
            ComboBox {
                id: primaryProvider
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
        if (visible)
            controller.loadSettingsStatus();
    }
}
