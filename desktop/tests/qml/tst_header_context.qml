import QtQuick
import QtTest
import "../../qml/components"

TestCase {
    id: testCase
    name: "HeaderContext"
    visible: true
    when: windowShown
    width: 1000
    height: 90

    HeaderContext {
        id: header
        width: testCase.width
        height: 34
        agentName: "Opus"
    }

    function test_missingModelNeverClaimsCurrentDefault() {
        header.metadata = {model:"", default_model:"a-different-model"};
        compare(findChild(header, "headerModel").text, "Model unavailable");
        header.metadata = {model:"original-model", default_model:"a-different-model"};
        compare(findChild(header, "headerModel").text, "original-model");
    }

    function test_statusModelEffortAndWorkspaceStayDistinct() {
        header.metadata = {state: "thinking", status_text: "Ready for review", model: "gpt-6-astra", effort: "high",
            workspace: {kind: "worktree", path: "/work/project-feature", label: "project / feature"}};
        compare(findChild(header, "headerJanitorStatus").text, "Ready for review");
        compare(findChild(header, "headerModel").text, "gpt-6-astra");
        compare(findChild(header, "headerEffort").text, "effort: high");
        compare(findChild(header, "headerWorkspaceLabel").text, "project / feature");
        verify(String(findChild(header, "headerWorkspaceIcon").source).endsWith("worktree.svg"));
        for (const width of [1000, 600, 360]) {
            header.width = width;
            waitForRendering(header);
            for (const name of ["headerAgentName", "headerWorkspaceLabel", "headerJanitorStatus", "headerModel", "headerEffort"]) {
                const item = findChild(header, name);
                const point = item.mapToItem(header, 0, 0);
                verify(point.x >= 0 && point.x + item.width <= header.width + 1, name + " overflows");
            }
        }
        header.metadata = {state: "idle", status_text: "Waiting for feedback", model: "opus", effort: "medium", workspace: {}};
        compare(findChild(header, "headerJanitorStatus").text, "Waiting for feedback");
        compare(findChild(header, "headerModel").text, "opus");
        header.width = testCase.width;
    }
}
