pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openChat(string session)
    color: "#171923"
    objectName: "updatesPanel"

    function isActiveJob(job) {
        const status = String(job.status || "");
        return status === "queued" || status === "running" || status === "active";
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "UPDATES"
                color: "#c9cde3"
                font.family: "JetBrains Mono"
                font.pixelSize: 17
                font.weight: Font.DemiBold
                font.letterSpacing: 1.6
            }
            Text {
                text: root.controller.attentionCount > 0
                    ? root.controller.attentionCount + " need attention" : "All caught up"
                color: root.controller.attentionCount > 0 ? "#c68b98" : "#687089"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
            }
            Item { Layout.fillWidth: true }
            BusyIndicator {
                running: root.controller.updatesLoading
                visible: running
                implicitWidth: 18
                implicitHeight: 18
            }
            ToolButton {
                text: "↻"
                implicitWidth: 28
                implicitHeight: 28
                onClicked: root.controller.loadUpdates()
                ToolTip.visible: hovered
                ToolTip.text: "Refresh updates"
            }
        }

        Rectangle {
            visible: root.controller.updatesError.length > 0
            Layout.fillWidth: true
            implicitHeight: visible ? errorText.implicitHeight + 14 : 0
            radius: 4
            color: "#2b2028"
            border.color: "#724655"
            Text {
                id: errorText
                anchors.fill: parent
                anchors.margins: 7
                text: root.controller.updatesError
                color: "#c98a98"
                wrapMode: Text.Wrap
                font.pixelSize: 10
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            ColumnLayout {
                width: Math.max(0, root.width - 36)
                spacing: 14

                Text {
                    visible: root.controller.attentionItems.length > 0
                    text: "NEEDS ATTENTION"
                    color: "#8d91aa"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1
                }

                Repeater {
                    model: root.controller.attentionItems

                    Rectangle {
                        id: decision
                        required property var modelData
                        readonly property bool actionPending: {
                            root.controller.updatesLoading;
                            return root.controller.updateActionPending(
                                "decision", String(modelData.decision_id || ""));
                        }
                        Layout.fillWidth: true
                        implicitHeight: decisionColumn.implicitHeight + 20
                        radius: 6
                        color: "#202334"
                        border.color: "#444a68"

                        ColumnLayout {
                            id: decisionColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 7

                            Text {
                                Layout.fillWidth: true
                                text: String(decision.modelData.title || "Decision")
                                color: "#d0d3e5"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: String(decision.modelData.question || decision.modelData.summary || "")
                                color: "#b8bbcf"
                                wrapMode: Text.Wrap
                                font.pixelSize: 11
                            }
                            Text {
                                visible: text.length > 0
                                Layout.fillWidth: true
                                text: String(decision.modelData.context || "")
                                color: "#74788f"
                                wrapMode: Text.Wrap
                                maximumLineCount: 4
                                elide: Text.ElideRight
                                font.pixelSize: 9
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: String(decision.modelData.agent_name || "") + "  ·  "
                                        + String(decision.modelData.session || "")
                                    color: "#666a81"
                                    font.family: "JetBrains Mono"
                                    font.pixelSize: 8
                                    elide: Text.ElideRight
                                }
                                Button {
                                    text: String(decision.modelData.no_label || "No")
                                    enabled: !decision.actionPending
                                    implicitHeight: 28
                                    onClicked: root.controller.resolveDecision(
                                        String(decision.modelData.decision_id || ""),
                                        "no", Number(decision.modelData.revision || 0))
                                }
                                Button {
                                    text: decision.actionPending ? "Resolving…"
                                        : String(decision.modelData.yes_label || "Yes")
                                    enabled: !decision.actionPending
                                    implicitHeight: 28
                                    onClicked: root.controller.resolveDecision(
                                        String(decision.modelData.decision_id || ""),
                                        "yes", Number(decision.modelData.revision || 0))
                                }
                            }
                        }
                    }
                }

                Text {
                    visible: root.controller.backgroundJobs.length > 0
                    text: "BACKGROUND JOBS"
                    color: "#8d91aa"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1
                }

                Repeater {
                    model: root.controller.backgroundJobs

                    Rectangle {
                        id: job
                        required property var modelData
                        readonly property bool actionPending: {
                            root.controller.updatesLoading;
                            return root.controller.updateActionPending(
                                "job", String(modelData.job_id || ""));
                        }
                        readonly property real progress: root.controller.backgroundJobProgress(modelData)
                        Layout.fillWidth: true
                        implicitHeight: jobRow.implicitHeight + 16
                        radius: 5
                        color: root.isActiveJob(job.modelData) ? "#1e2730" : "#1b1d28"
                        border.color: root.isActiveJob(job.modelData) ? "#405a58" : "#303347"

                        RowLayout {
                            id: jobRow
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 8
                            spacing: 9

                            Rectangle {
                                Layout.preferredWidth: 6
                                Layout.preferredHeight: 6
                                radius: 3
                                color: root.isActiveJob(job.modelData) ? "#89a879"
                                    : String(job.modelData.status || "") === "failed"
                                        ? "#bd7484" : "#656a80"
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    Layout.fillWidth: true
                                    text: String(job.modelData.title || job.modelData.kind || "Background job")
                                    color: "#c4c7da"
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: String(job.modelData.detail || "")
                                    color: "#6e7289"
                                    font.pixelSize: 9
                                    elide: Text.ElideRight
                                }
                                ProgressBar {
                                    visible: job.progress >= 0 && root.isActiveJob(job.modelData)
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: visible ? 3 : 0
                                    from: 0
                                    to: 1
                                    value: Math.max(0, job.progress)
                                }
                            }
                            Text {
                                text: String(job.modelData.status || "").toUpperCase()
                                color: root.isActiveJob(job.modelData) ? "#91aa85" : "#686d83"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 8
                            }
                            ToolButton {
                                visible: root.isActiveJob(job.modelData)
                                    && job.modelData.can_cancel !== false
                                text: "×"
                                enabled: !job.actionPending
                                implicitWidth: 26
                                implicitHeight: 26
                                onClicked: root.controller.cancelBackgroundJob(
                                    String(job.modelData.job_id || ""))
                                ToolTip.visible: hovered
                                ToolTip.text: "Cancel background job"
                            }
                        }
                    }
                }

                Text {
                    visible: root.controller.updateArtifacts.length > 0
                    text: "RECENT ARTIFACTS"
                    color: "#8d91aa"
                    font.family: "JetBrains Mono"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1
                }

                Repeater {
                    model: root.controller.updateArtifacts

                    Rectangle {
                        id: artifact
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: artifactRow.implicitHeight + 16
                        radius: 5
                        color: "#1b1d28"
                        border.color: "#303347"

                        RowLayout {
                            id: artifactRow
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 8
                            spacing: 9
                            Text {
                                text: String(artifact.modelData.type || "item").toUpperCase()
                                color: "#858baa"
                                font.family: "JetBrains Mono"
                                font.pixelSize: 8
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    Layout.fillWidth: true
                                    text: String(artifact.modelData.title || "Artifact")
                                    color: "#c3c6da"
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: String(artifact.modelData.summary || "")
                                    color: "#6b6f86"
                                    font.pixelSize: 9
                                    elide: Text.ElideRight
                                }
                            }
                            Button {
                                visible: String(artifact.modelData.session || "").length > 0
                                text: "Open chat"
                                implicitHeight: 27
                                onClicked: root.openChat(String(artifact.modelData.session))
                            }
                        }
                    }
                }

                ColumnLayout {
                    visible: !root.controller.updatesLoading
                        && root.controller.attentionItems.length === 0
                        && root.controller.backgroundJobs.length === 0
                        && root.controller.updateArtifacts.length === 0
                    Layout.fillWidth: true
                    Layout.topMargin: 80
                    spacing: 7
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "✓"
                        color: "#89a879"
                        font.pixelSize: 26
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Nothing needs you right now"
                        color: "#aeb2c9"
                        font.pixelSize: 12
                    }
                }
            }
        }
    }

    Timer {
        running: root.visible
        repeat: true
        interval: 10_000
        onTriggered: root.controller.loadUpdates()
    }

    onVisibleChanged: {
        if (visible)
            controller.loadUpdates();
    }
}
