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
    readonly property string title: String(cell.title || "Activity")
    readonly property string summary: String(cell.summary || "")
    readonly property string status: String(cell.status || "recorded")
    readonly property var lines: Array.from(cell.lines || [])
    readonly property int detailCount: Number(cell.detail_count || cell.detailCount || lines.length)
    readonly property color statusColor: status === "error" ? "#bd7484"
        : (status === "running" || status === "ok") ? "#89a879" : "#676b80"

    implicitHeight: cardColumn.implicitHeight + 6
    radius: 2
    color: hover.hovered ? "#222638" : "transparent"
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
                radius: 3
                color: root.statusColor
            }
            Text {
                visible: !explanation.enabled
                text: root.title
                color: "#9ea4c7"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Text {
                visible: !explanation.enabled
                Layout.fillWidth: true
                text: root.summary
                color: "#969bb5"
                font.pixelSize: 12
                elide: Text.ElideMiddle
            }
            Text {
                objectName: "activityExplanationText"
                visible: explanation.enabled
                Layout.fillWidth: true
                text: explanation.displayText
                textFormat: Text.PlainText
                color: "#82aaff"
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
            Text {
                visible: explanation.text.length > 0 || (!explanation.enabled && (root.lines.length > 0 || root.detailCount > 0))
                text: root.expanded ? "−" : "+"
                color: "#858aa7"
                font.pixelSize: 13
            }
        }

        ColumnLayout {
            visible: root.expanded && (!explanation.enabled || explanation.text.length > 0)
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
                color: "#aeb2ca"
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
                            return "#2b2028";
                        if (kind === "diff_new")
                            return "#1e2a27";
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
                            ? "#c98794" : "#aeb2ca"
                        font.family: "JetBrains Mono"
                        font.pixelSize: 12
                    }
                }
            }

            Text {
                visible: root.detailCount > root.lines.length
                text: (root.detailCount - root.lines.length) + " more details available"
                color: "#62667e"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
        }
    }

    TapHandler {
        enabled: explanation.text.length > 0 || (!explanation.enabled && (root.lines.length > 0 || root.detailCount > 0))
        onTapped: root.expanded = !root.expanded
    }
}
