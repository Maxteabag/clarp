import QtQuick
import QtQuick.Controls
import QtTest
import "../../qml/components"

TestCase {
    id: testCase
    name: "BackgroundProcesses"
    width: 520; height: 420; visible: true
    when: windowShown

    QtObject {
        id: stubController
        property bool showWhenReady: false
        property string selectedSession: "parent"
        property int avatarRevision: 0
        property int agentRevision: 0
        property int processRevision: 0
        property var avatarMotion: QtObject { property bool reducedMotion: true; property int revision: 0; function working(s) { return false; } function phase(s) { return 0; } function observe(o, v) {} }
        property var processes: ({
            jobs: [
                { jobId: "j1", kind: "watch", subAgent: false, title: "Watch the deploy", detail: "prod", status: "running", elapsed: "12m", heartbeat: "4s ago" },
                { jobId: "j2", kind: "sub-agent", subAgent: true, title: "Audit parser", detail: "", status: "running", elapsed: "3m", heartbeat: "" }
            ],
            helpers: [ { session: "helper-1", name: "Scout", statusText: "Reading files", state: "tool" } ],
            jobCount: 2, subAgentCount: 1, runningChildren: 1, total: 3
        })
        property string opened: ""
        function avatarSource(session) { return ""; }
        function chatStamp(timestamp) { return ""; }
        function agentName(session) { return session === "parent" ? "Parent" : session; }
        function agentProcesses(session) { return processes; }
        function selectSession(session) { opened = session; selectedSession = session; }
        function requestComposerFocus(pane) {}
    }

    Component {
        id: indicatorComponent
        ProcessIndicator { reducedMotion: true }
    }

    Component {
        id: popoverComponent
        ProcessPopover { controller: stubController; session: "parent" }
    }

    Component {
        id: rowComponent
        ChatRow {
            width: 360
            controller: stubController; session: "parent"; name: "Parent"; backend: "claude"
            workingDirectory: "/work"; avatarUrl: ""; lastMessage: ""; lastCompletedMessage: ""
            agentState: "background"; statusText: "2 background jobs running"; lastActivity: 0
            busy: false; unread: false; muted: false; queueCount: 0
            agentId: "p"; backgroundJobCount: 2; subAgentCount: 1; runningChildren: 1
            agentRole: "agent"; helperState: ""; treeDepth: 0
            doneHelpers: [ { parentAgentId: "p", count: 3, expanded: false, depth: 0 } ]
        }
    }

    Component {
        id: cellComponent
        DisplayCellCard { width: 460 }
    }

    function test_indicatorStates() {
        const indicator = createTemporaryObject(indicatorComponent, testCase);
        verify(!indicator.visible);
        compare(indicator.implicitWidth, 0);
        indicator.jobCount = 1;
        verify(indicator.visible);
        compare(indicator.glyphKind, "hourglass");
        verify(!findChild(indicator, "processIndicatorBadge").visible);
        compare(indicator.summary, "1 background job");
        indicator.jobCount = 2;
        indicator.subAgentCount = 1;
        indicator.runningChildren = 1;
        compare(indicator.total, 3);
        compare(indicator.glyphKind, "agent");
        const badge = findChild(indicator, "processIndicatorBadge");
        verify(badge.visible);
        compare(indicator.summary, "1 background job, 2 sub-agents");
        // A running helper alone switches to the agent glyph with no badge.
        indicator.jobCount = 0;
        indicator.subAgentCount = 0;
        compare(indicator.glyphKind, "agent");
        verify(!badge.visible);
        indicator.runningChildren = 12;
        compare(badge.children[0].text, "9+");
    }

    function test_indicatorClickRequestsTheList() {
        const indicator = createTemporaryObject(indicatorComponent, testCase, { jobCount: 1, width: 16, height: 16 });
        const spy = createTemporaryObject(signalSpy, testCase, { target: indicator, signalName: "clicked" });
        mouseClick(indicator);
        compare(spy.count, 1);
    }

    Component { id: signalSpy; SignalSpy {} }

    function test_popoverListsProcessesAndClosesOnEscapeAndOutside() {
        const popover = createTemporaryObject(popoverComponent, testCase);
        popover.openFor("parent", testCase);
        tryCompare(popover, "opened", true);
        compare(popover.jobs.length, 2);
        compare(popover.helpers.length, 1);
        compare(findChild(popover.contentItem, "processPopoverCount").text, "3 running");
        keyClick(Qt.Key_Escape);
        tryCompare(popover, "visible", false);

        popover.openFor("parent", testCase);
        tryCompare(popover, "opened", true);
        mouseClick(testCase, testCase.width - 5, testCase.height - 5);
        tryCompare(popover, "visible", false);
    }

    function test_popoverHelperOpensItsChat() {
        stubController.opened = "";
        const popover = createTemporaryObject(popoverComponent, testCase);
        popover.openFor("parent", testCase);
        tryCompare(popover, "opened", true);
        let helper = null;
        const stack = [popover.contentItem];
        while (stack.length > 0 && helper === null) {
            const item = stack.pop();
            if (item.objectName === "processHelperRow") helper = item;
            for (let i = 0; i < item.children.length; ++i) stack.push(item.children[i]);
        }
        verify(helper !== null);
        mouseClick(helper);
        compare(stubController.opened, "helper-1");
        tryCompare(popover, "visible", false);
    }

    function test_rowShowsIndicatorAndDoneHelpersLine() {
        const row = createTemporaryObject(rowComponent, testCase);
        const indicator = findChild(row, "sidebarProcessIndicator");
        verify(indicator.visible);
        compare(indicator.total, 3);
        const requested = createTemporaryObject(signalSpy, testCase, { target: row, signalName: "processesRequested" });
        const selected = createTemporaryObject(signalSpy, testCase, { target: row, signalName: "chatSelected" });
        mouseClick(indicator);
        compare(requested.count, 1);
        compare(requested.signalArguments[0][0], "parent");
        compare(selected.count, 0); // The indicator does not also open the chat.

        const line = findChild(row, "doneHelpersLine");
        verify(line !== null);
        compare(line.text, "▸ 3 helpers done");
        const toggled = createTemporaryObject(signalSpy, testCase, { target: row, signalName: "doneHelpersToggled" });
        mouseClick(line);
        compare(toggled.count, 1);
        compare(toggled.signalArguments[0][0], "p");
        compare(selected.count, 0);

        row.treeDepth = 1;
        compare(row.leftPadding, 14 + 22);
        verify(findChild(row, "helperConnector").visible);
        row.backgroundJobCount = 0; row.subAgentCount = 0; row.runningChildren = 0;
        verify(!indicator.visible);
    }

    function test_subagentCellShowsPhaseNameAndTask() {
        const card = createTemporaryObject(cellComponent, testCase, { cell: {
            kind: "subagents", title: "Spawning agent", summary: "Kepler (explorer)", status: "running",
            lines: [ { label: "Task", text: "Audit the parser" } ],
            _subagent: { phase: "spawned", running: true, name: "Kepler (explorer)", task: "Audit the parser" }
        } });
        verify(findChild(card, "subagentCellGlyph").visible);
        compare(findChild(card, "subagentCellPhase").text, "spawned");
        compare(findChild(card, "displayCellTitle").text, "Kepler (explorer)");
        compare(findChild(card, "displayCellSummary").text, "Audit the parser");

        const plain = createTemporaryObject(cellComponent, testCase, { cell: {
            kind: "command", title: "Ran command", summary: "make test", status: "ok", lines: []
        } });
        verify(!findChild(plain, "subagentCellGlyph").visible);
        verify(!findChild(plain, "subagentCellPhase").visible);
        compare(findChild(plain, "displayCellTitle").text, "Ran command");
        compare(findChild(plain, "displayCellSummary").text, "make test");
    }
}
