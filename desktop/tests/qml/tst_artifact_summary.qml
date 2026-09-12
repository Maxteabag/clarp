import QtQuick
import QtTest
import "../../qml/components"
Item {
    width: 700; height: 500
    ArtifactSummaryCard { id: card; width: 680; readable: true; artifact: ({artifact_id: "report", type: "research", title: "Review", summary: "Findings", status: "ready", session: "mike"}) }
    SignalSpy { id: opened; target: card; signalName: "openRequested" }
    SignalSpy { id: chat; target: card; signalName: "chatRequested" }
    TestCase {
        name: "ArtifactSummary"; when: windowShown
        function test_actionsAndFailures() {
            mouseClick(findChild(card, "artifactViewReport")); compare(opened.signalArguments[0][0], "report")
            mouseClick(findChild(card, "artifactOpenChat")); compare(chat.signalArguments[0][0], "mike")
            card.artifact = {type: "workflow_run", status: "completed", conclusion: "failure", total_steps: 6, completed_steps: 4}
            compare(card.failed, true); compare(findChild(card, "artifactOutcome").text, "failure")
            compare(findChild(card, "artifactProgress").text, "4 / 6 completed")
        }
        function test_nestedPlanAndUnknownTotal() {
            card.artifact = {type: "plan", status: "active", plan: {total_count: 8, completed_count: 2}}
            compare(findChild(card, "artifactProgress").text, "2 / 8 completed")
            card.artifact = {type: "plan", status: "active"}
            compare(findChild(card, "artifactProgress").text, "In progress")
        }
    }
}
