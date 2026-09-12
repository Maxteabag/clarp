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
    color: "#1a1b26"
    border.color: failed ? "#aa675f" : "#394154"
    radius: 6
    ColumnLayout {
        id: body
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.margins: 12
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            TuiText { text: root.kind.replace(/_/g, " ").toUpperCase(); color: "#929bb3"; font.pixelSize: 11 }
            Item { Layout.fillWidth: true }
            TuiText { objectName: "artifactOutcome"; text: root.outcomeState; color: root.failed ? "#f2a39a" : "#a5c6a8" }
        }
        TuiText {
            objectName: "artifactTitle"
            text: String(root.artifact.file_name || root.artifact.title || "Artifact")
            Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 15; color: "#e4e6ef"
        }
        TuiText {
            visible: text.length > 0
            text: String(root.artifact.summary || "")
            Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 2
            elide: Text.ElideRight; color: "#abb2c7"
        }
        TuiText {
            objectName: "artifactProgress"
            visible: ["plan", "workflow_run"].includes(root.kind)
            text: root.total > 0 ? root.completed + " / " + root.total + " completed" : (root.outcomeState === "active" ? "In progress" : "")
            color: "#b5c7b8"
        }
        TuiText {
            visible: root.kind === "countdown" && text.length > 0
            text: String(root.artifact.target_at || "") + (root.artifact.time_zone ? " · " + root.artifact.time_zone : "")
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#b5c7b8"
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
                text: "Interactive form unavailable"; color: "#e3bb87"
                wrapMode: Text.Wrap; Layout.fillWidth: true
            }
        }
    }
}
