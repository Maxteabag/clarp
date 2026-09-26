pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    objectName: "displayCellCard"

    required property var cell
    property var narrator: null
    property string workingDirectory: ""
    property string session: ""
    property bool localFilesAllowed: false
    property bool expanded: false
    readonly property int explanationRepeat: Number(cell._explanationRepeat ?? 1)
    readonly property string title: String(cell.title || "Activity")
    readonly property string summary: String(cell.summary || "")
    readonly property string status: String(cell.status || "recorded")
    readonly property var lines: Array.from(cell.lines || [])
    readonly property int detailCount: Number(cell.detail_count || cell.detailCount || lines.length)
    // Harness sub-agent cells (Claude Agent/Task, Codex spawn_agent) get the
    // agent glyph, a phase label and the sub-agent's task beside its name.
    readonly property var subagent: cell._subagent || null
    readonly property bool isSubagent: root.subagent !== null && root.subagent !== undefined
    readonly property string subagentPhase: root.isSubagent ? String(root.subagent.phase || "") : ""
    readonly property color statusColor: status === "error" ? Theme.danger
        : (status === "running" || status === "ok") ? Theme.secondary : Theme.muted

    visible: explanationRepeat !== 0
    implicitHeight: cardColumn.implicitHeight + 6
    radius: Theme.radius
    color: hover.hovered ? Theme.control : "transparent"
    border.width: 0
    HoverHandler { id: hover }

    ActivityExplanation {
        session: root.session
        id: explanation
        narrator: root.narrator
        activity: root.cell
        active: root.visible
        workingDirectory: root.workingDirectory
        localFilesAllowed: root.localFilesAllowed
    }

    ColumnLayout {
        id: cardColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: 3
        anchors.rightMargin: 3
        anchors.topMargin: 3
        spacing: 3

        RowLayout {
            Layout.fillWidth: true
            spacing: 7

            Rectangle {
                visible: !root.isSubagent
                Layout.preferredWidth: 6
                Layout.preferredHeight: 6
                radius: Theme.radius
                color: root.statusColor
            }
            ProcessGlyph {
                objectName: "subagentCellGlyph"
                visible: root.isSubagent
                Layout.preferredWidth: visible ? 13 : 0
                glyphSize: 13
                kind: "agent"
                color: root.subagentPhase === "failed" ? Theme.danger
                    : root.subagentPhase === "finished" ? Theme.muted : Theme.link
                running: root.isSubagent && Boolean(root.subagent.running)
            }
            TuiText {
                objectName: "subagentCellPhase"
                visible: root.isSubagent && !explanation.narrationShown
                text: root.subagentPhase === "activity" ? "sub-agent" : root.subagentPhase
                color: root.subagentPhase === "failed" ? Theme.danger
                    : root.subagentPhase === "finished" ? Theme.muted : Theme.link
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
            TuiText {
                visible: !explanation.narrationShown
                text: root.isSubagent ? String(root.subagent.name || root.title) : root.title
                objectName: "displayCellTitle"
                textFormat: Text.PlainText
                elide: Text.ElideRight
                Layout.maximumWidth: root.isSubagent ? root.width * 0.4 : Number.POSITIVE_INFINITY
                color: Theme.text
                font.family: "JetBrains Mono"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            TuiText {
                objectName: "displayCellSummary"
                visible: !explanation.narrationShown
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.isSubagent ? String(root.subagent.task || root.title) : root.summary
                color: Theme.muted
                font.pixelSize: 12
                elide: Text.ElideMiddle
            }
            TuiText {
                objectName: "activityExplanationText"
                visible: explanation.narrationShown
                Layout.fillWidth: true
                text: explanation.displayText + (root.explanationRepeat > 1 ? " (x" + root.explanationRepeat + ")" : "")
                textFormat: Text.PlainText
                color: Theme.link
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
            TuiText {
                visible: explanation.text.length > 0 || (!explanation.narrationShown && (root.lines.length > 0 || root.detailCount > 0))
                text: root.expanded ? "−" : "+"
                color: Theme.secondary
                font.pixelSize: 13
            }
        }

        ColumnLayout {
            visible: root.expanded && (!explanation.narrationShown || explanation.text.length > 0)
            Layout.fillWidth: true
            Layout.leftMargin: 15
            spacing: 0

            TextEdit {
                visible: explanation.text.length > 0
                Layout.fillWidth: true
                text: root.title + " · " + root.summary
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: Theme.secondary
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }

            Repeater {
                model: root.lines

                Rectangle {
                    id: lineRow
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: lineText.implicitHeight + 4
                    radius: Theme.radius
                    color: {
                        const kind = String(lineRow.modelData.kind || "");
                        if (kind === "diff_old")
                            return Theme.dangerSurface;
                        if (kind === "diff_new")
                            return Qt.alpha(Theme.success, 0.16);
                        return "transparent";
                    }

                    TextEdit {
                        id: lineText
                        anchors.fill: parent
                        anchors.margins: 2
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        text: {
                            const label = String(lineRow.modelData.label || "");
                            const value = String(lineRow.modelData.text || "");
                            return label.length > 0 ? label + "  " + value : value;
                        }
                        color: String(lineRow.modelData.kind || "") === "error"
                            ? Theme.danger : Theme.secondary
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                    }
                }
            }

            TuiText {
                visible: root.detailCount > root.lines.length
                text: (root.detailCount - root.lines.length) + " more details available"
                color: Theme.muted
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
        }
    }

    TapHandler {
        enabled: explanation.text.length > 0 || (!explanation.narrationShown && (root.lines.length > 0 || root.detailCount > 0))
        onTapped: root.expanded = !root.expanded
    }
}
