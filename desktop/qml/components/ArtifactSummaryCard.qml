pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts
Rectangle {
    id: root
    required property var artifact
    property bool readable: false
    signal openRequested(string artifactId)
    signal chatRequested(string session)
    readonly property string kind: String(artifact.type || "item")
    readonly property string outcomeState: String(artifact.conclusion || artifact.status || "unknown")
    readonly property bool failed: ["failed", "failure", "timed_out", "action_required"].includes(outcomeState)
    readonly property var plan: artifact.plan || ({})
    readonly property int total: Math.max(0, Number(kind === "plan" ? plan.total_count || 0 : artifact.total_steps || 0))
    readonly property int completed: Math.max(0, Number(kind === "plan" ? plan.completed_count || 0 : artifact.completed_steps || 0))
    implicitHeight: body.implicitHeight + 24
    color: Theme.window
    border.color: failed ? Theme.danger : Theme.border
    radius: 6
    ColumnLayout {
        id: body
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.margins: 12
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            TuiText { text: root.kind.replace(/_/g, " ").toUpperCase(); color: Theme.secondary; font.pixelSize: 11 }
            Item { Layout.fillWidth: true }
            TuiText { objectName: "artifactOutcome"; text: root.outcomeState; color: root.failed ? Theme.danger : Theme.text }
        }
        TuiText {
            objectName: "artifactTitle"
            text: String(root.artifact.file_name || root.artifact.title || "Artifact")
            Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 15; color: Theme.text
        }
        TuiText {
            visible: text.length > 0
            text: String(root.artifact.summary || "")
            Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 2
            elide: Text.ElideRight; color: Theme.text
        }
        TuiText {
            objectName: "artifactProgress"
            visible: ["plan", "workflow_run"].includes(root.kind)
            text: root.total > 0 ? root.completed + " / " + root.total + " completed" : (root.outcomeState === "active" ? "In progress" : "")
            color: Theme.text
        }
        TuiText {
            visible: root.kind === "countdown" && text.length > 0
            text: String(root.artifact.target_at || "") + (root.artifact.time_zone ? " · " + root.artifact.time_zone : "")
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.text
        }
        RowLayout {
            TuiButton {
                objectName: "artifactViewReport"; visible: root.readable
                text: "Read"; Accessible.name: "Read " + String(root.artifact.title || "artifact")
                onClicked: root.openRequested(String(root.artifact.artifact_id || ""))
            }
            TuiButton {
                objectName: "artifactOpenChat"; visible: String(root.artifact.session || "").length > 0
                text: "Open chat"; onClicked: root.chatRequested(String(root.artifact.session))
            }
            TuiText {
                visible: root.kind === "html_form"
                text: "Interactive form unavailable"; color: Theme.warning
                wrapMode: Text.Wrap; Layout.fillWidth: true
            }
        }
    }
}
