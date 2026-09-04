pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var cell
    property bool expanded: false
    readonly property string title: String(cell.title || "Activity")
    readonly property string summary: String(cell.summary || "")
    readonly property string status: String(cell.status || "recorded")
    readonly property var lines: Array.isArray(cell.lines) ? cell.lines : []
    readonly property int detailCount: Number(cell.detail_count || cell.detailCount || lines.length)
    readonly property color statusColor: status === "error" ? "#bd7484"
        : (status === "running" || status === "ok") ? "#89a879" : "#676b80"

    implicitHeight: cardColumn.implicitHeight + 14
    radius: 5
    color: "#171923"
    border.color: expanded ? "#596083" : "#303347"

    ColumnLayout {
        id: cardColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 7
        spacing: 5

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
                text: root.title.toUpperCase()
                color: "#9ea4c7"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: root.summary
                color: "#6e728b"
                font.pixelSize: 10
                elide: Text.ElideMiddle
            }
            Text {
                visible: root.lines.length > 0 || root.detailCount > 0
                text: root.expanded ? "−" : "+"
                color: "#858aa7"
                font.pixelSize: 13
            }
        }

        ColumnLayout {
            visible: root.expanded
            Layout.fillWidth: true
            spacing: 4

            Repeater {
                model: root.lines

                Rectangle {
                    id: lineRow
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: lineText.implicitHeight + 8
                    radius: 3
                    color: {
                        const kind = String(lineRow.modelData.kind || "");
                        if (kind === "diff_old")
                            return "#2b2028";
                        if (kind === "diff_new")
                            return "#1e2a27";
                        return "#13151d";
                    }

                    TextEdit {
                        id: lineText
                        anchors.fill: parent
                        anchors.margins: 4
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
                        font.pixelSize: 9
                    }
                }
            }

            Text {
                visible: root.detailCount > root.lines.length
                text: (root.detailCount - root.lines.length) + " more details available"
                color: "#62667e"
                font.family: "JetBrains Mono"
                font.pixelSize: 8
            }
        }
    }

    TapHandler {
        enabled: root.lines.length > 0 || root.detailCount > 0
        onTapped: root.expanded = !root.expanded
    }
}
