pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property string agentName
    property var metadata: ({})
    property bool showRuntime: true
    readonly property var workspace: root.metadata.workspace || ({})
    implicitHeight: 28

    RowLayout {
        anchors.fill: parent
        spacing: 12
        TuiText {
            textFormat: Text.PlainText
            objectName: "headerAgentName"
            text: root.agentName || "No agent selected"
            font.family: "JetBrains Mono"
            font.pixelSize: 13
            font.weight: Font.DemiBold
            color: "#e7e1dc"
            elide: Text.ElideRight
            Layout.minimumWidth: 0
            Layout.maximumWidth: root.showRuntime ? root.width * 0.20 : root.width
            HoverHandler { id: nameHover }
            ToolTip.visible: nameHover.hovered
            ToolTip.text: root.agentName
        }
        RowLayout {
            visible: String(root.workspace.path || "").length > 0
            Layout.fillWidth: true
            Layout.minimumWidth: 36
            spacing: 5
            Image {
                objectName: "headerWorkspaceIcon"
                Layout.preferredWidth: 16
                Layout.preferredHeight: 16
                source: Qt.resolvedUrl("../../resources/icons/" + (root.workspace.kind || "directory") + ".svg")
            }
            TuiText {
                textFormat: Text.PlainText
                objectName: "headerWorkspaceLabel"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: String(root.workspace.label || root.workspace.path || "")
                color: "#a6adc8"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
                elide: Text.ElideMiddle
                HoverHandler { id: workspaceHover }
                ToolTip.visible: workspaceHover.hovered
                ToolTip.text: (root.workspace.kind === "worktree" ? "Worktree" : root.workspace.kind === "repo" ? "Repository" : "Directory")
                    + ": " + String(root.workspace.path || "")
            }
        }
        Item { visible: !String(root.workspace.path || "").length; Layout.fillWidth: true }
        TuiText {
            textFormat: Text.PlainText
            objectName: "headerJanitorStatus"
            visible: root.showRuntime && text.length > 0
            text: String(root.metadata.status_text || "")
            Layout.minimumWidth: 0
            Layout.maximumWidth: root.width * 0.28
            elide: Text.ElideRight
            color: "#b1bd96"
            font.family: "JetBrains Mono"
            font.pixelSize: 11
            HoverHandler { id: statusHover }
            ToolTip.visible: statusHover.hovered
            ToolTip.text: "Status: " + String(root.metadata.status_text || "")
        }
        TuiText {
            textFormat: Text.PlainText
            objectName: "headerModel"
            visible: root.showRuntime
            text: String(root.metadata.model || "default model")
            Layout.minimumWidth: 0
            Layout.maximumWidth: root.width * 0.19
            elide: Text.ElideMiddle
            color: "#a6adc8"
            font.family: "JetBrains Mono"
            font.pixelSize: 11
            HoverHandler { id: modelHover }
            ToolTip.visible: modelHover.hovered
            ToolTip.text: "Configured model: " + String(root.metadata.model || "Host default")
        }
        TuiText {
            textFormat: Text.PlainText
            objectName: "headerEffort"
            visible: root.showRuntime
            text: "effort: " + String(root.metadata.effort || "default")
            HoverHandler { id: effortHover }
            ToolTip.visible: effortHover.hovered
            ToolTip.text: "Configured effort: " + String(root.metadata.effort || "Host default")
            Layout.minimumWidth: 0
            Layout.maximumWidth: root.width * 0.15
            elide: Text.ElideRight
            color: "#8d93aa"
            font.family: "JetBrains Mono"
            font.pixelSize: 11
        }
    }
}
