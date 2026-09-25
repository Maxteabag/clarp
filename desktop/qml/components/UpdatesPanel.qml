pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openChat(string session)
    signal openReport(string artifactId)
    color: Theme.window
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
            TuiText {
                text: "UPDATES"
                color: Theme.text
                font.family: "JetBrains Mono"
                font.pixelSize: 17
                font.weight: Font.DemiBold
                font.letterSpacing: 1.6
            }
            TuiText {
                text: root.controller.attentionCount > 0
                    ? root.controller.attentionCount + " need attention" : "All caught up"
                color: root.controller.attentionCount > 0 ? Theme.danger : Theme.muted
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
            Item { Layout.fillWidth: true }
            TuiBusyIndicator {
                running: root.controller.updatesLoading
                visible: running
                implicitWidth: 18
                implicitHeight: 18
            }
            TuiToolButton {
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
            radius: Theme.radius
            color: Theme.dangerSurface
            border.color: Qt.alpha(Theme.danger, 0.55)
            TuiText {
                id: errorText
                anchors.fill: parent
                anchors.margins: 7
                text: root.controller.updatesError
                color: Theme.danger
                wrapMode: Text.Wrap
                font.pixelSize: 12
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            ColumnLayout {
                width: Math.max(0, root.width - 36)
                spacing: 14

                TuiText {
                    visible: root.controller.attentionItems.length > 0
                    text: "NEEDS ATTENTION"
                    color: Theme.secondary
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
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
                        radius: Theme.radius
                        color: Theme.control
                        border.width: 0

                        ColumnLayout {
                            id: decisionColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 7

                            TuiText {
                                Layout.fillWidth: true
                                text: String(decision.modelData.title || "Decision")
                                color: Theme.text
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            TuiText {
                                Layout.fillWidth: true
                                text: String(decision.modelData.question || decision.modelData.summary || "")
                                color: Theme.secondary
                                wrapMode: Text.Wrap
                                font.pixelSize: 13
                            }
                            TuiText {
                                visible: text.length > 0
                                Layout.fillWidth: true
                                text: String(decision.modelData.context || "")
                                color: Theme.muted
                                wrapMode: Text.Wrap
                                maximumLineCount: 4
                                elide: Text.ElideRight
                                font.pixelSize: 11
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                TuiText {
                                    Layout.fillWidth: true
                                    text: String(decision.modelData.agent_name || "") + "  ·  "
                                        + String(decision.modelData.session || "")
                                    color: Theme.muted
                                    font.family: "JetBrains Mono"
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                                TuiButton {
                                    text: String(decision.modelData.no_label || "No")
                                    enabled: !decision.actionPending
                                    implicitHeight: 28
                                    onClicked: root.controller.resolveDecision(
                                        String(decision.modelData.decision_id || ""),
                                        "no", Number(decision.modelData.revision || 0))
                                }
                                TuiButton {
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

                TuiText {
                    visible: root.controller.backgroundJobs.length > 0
                    text: "BACKGROUND JOBS"
                    color: Theme.secondary
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
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
                        radius: Theme.radius
                        color: "transparent"
                        border.width: 0

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
                                radius: Theme.radius
                                color: root.isActiveJob(job.modelData) ? Theme.secondary
                                    : String(job.modelData.status || "") === "failed"
                                        ? Theme.danger : Theme.muted
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                TuiText {
                                    Layout.fillWidth: true
                                    text: String(job.modelData.title || job.modelData.kind || "Background job")
                                    color: Theme.text
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                TuiText {
                                    Layout.fillWidth: true
                                    text: String(job.modelData.detail || "")
                                    color: Theme.muted
                                    font.pixelSize: 11
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
                            TuiText {
                                text: String(job.modelData.status || "").toUpperCase()
                                color: root.isActiveJob(job.modelData) ? Theme.secondary : Theme.muted
                                font.family: "JetBrains Mono"
                                font.pixelSize: 11
                            }
                            TuiToolButton {
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

                TuiText {
                    visible: root.controller.updateArtifacts.length > 0
                    text: "RECENT ARTIFACTS"
                    color: Theme.secondary
                    font.family: "JetBrains Mono"
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1
                }

                Repeater {
                    model: root.controller.updateArtifacts

                    ArtifactSummaryCard {
                        required property var modelData
                        Layout.fillWidth: true
                        artifact: modelData
                        readable: root.controller.artifactIsViewableReport(modelData)
                        onOpenRequested: artifactId => root.openReport(artifactId)
                        onChatRequested: session => root.openChat(session)
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
                    TuiText {
                        Layout.alignment: Qt.AlignHCenter
                        text: "✓"
                        color: Theme.secondary
                        font.pixelSize: 17
                    }
                    TuiText {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Nothing needs you right now"
                        color: Theme.text
                        font.pixelSize: 14
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
