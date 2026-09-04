pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    required property var tool
    property bool expanded: false
    readonly property string toolName: String(tool.name || tool.action || "Tool")
    readonly property string summary: String(tool.summary || tool.description || tool.file_path || "")
    readonly property string status: String(tool.status || "recorded")
    readonly property string detail: {
        const parts = [];
        if (tool.command)
            parts.push(String(tool.command));
        else if (tool.input)
            parts.push(typeof tool.input === "string" ? tool.input : JSON.stringify(tool.input, null, 2));
        if (tool.result)
            parts.push(String(tool.result));
        return parts.join("\n\n");
    }
    readonly property color statusColor: status === "error" ? "#df7777" : status === "running" ? "#e7aa68" : "#6fbd98"

    implicitHeight: toolColumn.implicitHeight + 14
    radius: 5
    color: "#171923"
    border.color: expanded ? "#596083" : "#303347"

    ColumnLayout {
        id: toolColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 7
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Rectangle {
                Layout.preferredWidth: 7
                Layout.preferredHeight: 7
                radius: 4
                color: root.statusColor
            }
            Text {
                text: root.toolName
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
                elide: Text.ElideRight
            }
            Text {
                visible: root.detail.length > 0
                text: root.expanded ? "−" : "+"
                color: "#858aa7"
                font.pixelSize: 13
            }
        }

        Rectangle {
            visible: root.expanded && root.detail.length > 0
            Layout.fillWidth: true
            implicitHeight: detailText.implicitHeight + 16
            radius: 3
            color: "#13151d"

            TextEdit {
                id: detailText
                anchors.fill: parent
                anchors.margins: 8
                text: root.detail
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                color: "#aeb2ca"
                selectionColor: "#565d82"
                font.family: "JetBrains Mono"
                font.pixelSize: 9
            }
        }
    }

    TapHandler {
        enabled: root.detail.length > 0
        onTapped: root.expanded = !root.expanded
    }
}
