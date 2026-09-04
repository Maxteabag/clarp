import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal closeRequested
    color: "#e6121116"

    property bool loaded: false

    Connections {
        target: root.controller
        function onOrchestratorChanged() {
            if (root.controller.orchestratorLoading)
                return;
            const settings = root.controller.orchestratorSettings;
            enabledSwitch.checked = Boolean(settings.enabled);
            fallbackSwitch.checked = settings.fallback_only !== false;
            confidenceSlider.value = Number(settings.confidence_threshold || 0.78);
            providerBox.currentIndex = Math.max(0, providerBox.model.indexOf(String(settings.provider || "openai")));
            modelField.text = String(settings.model || "");
            effortBox.currentIndex = Math.max(0, effortBox.model.indexOf(String(settings.effort || "")));
            timeoutField.value = Number(settings.timeout_ms || 30000);
            root.loaded = true;
        }
    }

    MouseArea {
        anchors.fill: parent
        onClicked: mouse => mouse.accepted = true
    }

    Rectangle {
        width: Math.min(590, parent.width - 48)
        implicitHeight: contentColumn.implicitHeight + 56
        anchors.centerIn: parent
        radius: 20
        color: "#1b1820"
        border.color: "#403648"

        ColumnLayout {
            id: contentColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: 28
            spacing: 13

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: "Orchestrator"
                    color: "#f1ebe6"
                    font.pixelSize: 21
                    font.weight: Font.DemiBold
                }
                BusyIndicator {
                    visible: root.controller.orchestratorLoading
                    running: visible
                    implicitWidth: 22
                    implicitHeight: 22
                }
                ToolButton {
                    text: "×"
                    onClicked: root.closeRequested()
                }
            }

            Text {
                Layout.fillWidth: true
                text: "Name matching runs first. The routing model resolves delegations that do not clearly select one agent."
                color: "#928a98"
                wrapMode: Text.Wrap
                font.pixelSize: 12
            }

            Switch {
                id: enabledSwitch
                text: "Use AI to resolve failed delegations"
            }
            Switch {
                id: fallbackSwitch
                text: "Only when name matching cannot decide"
            }

            Label {
                text: "Automatic routing confidence  " + confidenceSlider.value.toFixed(2)
                color: "#aaa2ae"
            }
            Slider {
                id: confidenceSlider
                Layout.fillWidth: true
                from: 0.5
                to: 0.99
                stepSize: 0.01
                value: 0.78
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                ColumnLayout {
                    Layout.fillWidth: true
                    Label {
                        text: "Provider"
                        color: "#aaa2ae"
                    }
                    ComboBox {
                        id: providerBox
                        Layout.fillWidth: true
                        model: ["openai", "claude", "codex", "agy"]
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Label {
                        text: "Effort"
                        color: "#aaa2ae"
                    }
                    ComboBox {
                        id: effortBox
                        Layout.fillWidth: true
                        model: ["", "minimal", "low", "medium", "high", "xhigh"]
                    }
                }
            }

            Label {
                text: "Model override"
                color: "#aaa2ae"
            }
            TextField {
                id: modelField
                Layout.fillWidth: true
                placeholderText: "Provider default"
            }

            Label {
                text: "Timeout (milliseconds)"
                color: "#aaa2ae"
            }
            SpinBox {
                id: timeoutField
                from: 250
                to: 60000
                stepSize: 250
                value: 30000
                editable: true
            }

            Rectangle {
                visible: root.controller.orchestratorLastDecision.length > 0
                Layout.fillWidth: true
                implicitHeight: lastDecision.implicitHeight + 18
                radius: 9
                color: "#151319"
                Text {
                    id: lastDecision
                    anchors.fill: parent
                    anchors.margins: 9
                    text: "Last decision: " + root.controller.orchestratorLastDecision
                    color: "#7f7785"
                    wrapMode: Text.Wrap
                    font.pixelSize: 11
                }
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
                    text: "Save"
                    enabled: root.loaded && !root.controller.orchestratorLoading
                    onClicked: {
                        root.controller.saveOrchestrator(enabledSwitch.checked, fallbackSwitch.checked, confidenceSlider.value, providerBox.currentText, modelField.text, effortBox.currentText, timeoutField.value);
                        root.closeRequested();
                    }
                }
            }
        }
    }
}
