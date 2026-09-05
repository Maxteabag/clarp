pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    objectName: "toolCard"

    required property var tool
    property var narrator: null
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

    implicitHeight: toolColumn.implicitHeight + 6
    radius: 2
    color: hover.hovered ? "#222638" : "transparent"
    border.width: 0
    HoverHandler { id: hover }

    ActivityExplanation {
        id: explanation
        narrator: root.narrator
        activity: root.tool
        active: root.visible
    }

    ColumnLayout {
        id: toolColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: 3
        anchors.rightMargin: 3
        anchors.topMargin: 3
        spacing: 3

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
                visible: explanation.text.length === 0
                text: root.toolName
                color: "#9ea4c7"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Text {
                visible: explanation.text.length === 0
                Layout.fillWidth: true
                text: root.summary
                color: "#969bb5"
                font.pixelSize: 12
                elide: Text.ElideRight
            }
            Text {
                objectName: "activityExplanationText"
                visible: explanation.text.length > 0
                Layout.fillWidth: true
                text: explanation.text
                textFormat: Text.PlainText
                color: "#82aaff"
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
            Text {
                visible: root.detail.length > 0 || explanation.text.length > 0
                text: root.expanded ? "−" : "+"
                color: "#858aa7"
                font.pixelSize: 13
            }
        }

        Rectangle {
            visible: root.expanded && (root.detail.length > 0 || explanation.text.length > 0)
            Layout.fillWidth: true
            Layout.leftMargin: 15
            implicitHeight: detailText.implicitHeight + 6
            color: "transparent"

            TextEdit {
                id: detailText
                anchors.fill: parent
                anchors.margins: 3
                text: (explanation.text.length > 0 ? root.toolName + " · " + root.summary + "\n\n" : "") + root.detail
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                color: "#aeb2ca"
                selectionColor: "#565d82"
                font.family: "JetBrains Mono"
                font.pixelSize: 12
            }
        }
    }

    TapHandler {
        enabled: root.detail.length > 0 || explanation.text.length > 0
        onTapped: root.expanded = !root.expanded
    }
}
