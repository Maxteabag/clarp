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
    readonly property color statusColor: status === "error" ? Theme.danger
        : (status === "running" || status === "ok") ? Theme.secondary : Theme.muted

    visible: explanationRepeat !== 0
    implicitHeight: cardColumn.implicitHeight + 6
    radius: 0
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
                Layout.preferredWidth: 6
                Layout.preferredHeight: 6
                radius: 0
                color: root.statusColor
            }
            TuiText {
                visible: !explanation.narrationShown
                text: root.title
                color: Theme.text
                font.family: "JetBrains Mono"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            TuiText {
                visible: !explanation.narrationShown
                Layout.fillWidth: true
                text: root.summary
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
                    radius: 0
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
