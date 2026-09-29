// needs: fake-host
// Port of tst_native_core::controllerTracksJobsFromListAndEvents plus the
// attention, artifacts and next-attention wiring.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var revision: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function post(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    function job(id, status, kind) {
        const now = Date.now()
        return {job_id: id, agent_id: "a1", session: "rachel", kind: kind || "watch", title: id + " title",
                status: status, started_at: now - 1000, heartbeat_at: now, updated_at: now}
    }
    // AgentListModel roles: UserRole+1+index in the C++ enum order.
    readonly property int backgroundJobCountRole: 256 + 1 + 33
    readonly property int subAgentCountRole: 256 + 1 + 34
    function rachel(role) { return app.agents.data(app.agents.index(app.agents.indexOfSession("rachel"), 0), role) }

    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            post("/__control/jobs", {jobs: [job("watch", "running"), job("scout", "running", "sub-agent")]})
            app.loadUpdates()
            stage = 1
        } else if (stage === 1 && !app.updatesLoading && rachel(backgroundJobCountRole) === 2) {
            check(rachel(subAgentCountRole) === 1, "the list replaces the snapshot's counts")
            check(app.agentProcesses("rachel").jobs.length === 2, "process popover lists both jobs")
            check(app.attentionCount === 1 && app.updateArtifacts.length === 1, "attention and artifacts loaded")
            check(app.nextAttentionTarget === "mike", "a pending decision makes its chat the next target")
            check(app.backgroundJobProgressText(JSON.stringify({metadata: {completed: 3, total: 10}})) === 0.3, "job progress")
            revision = app.processRevision
            const done = job("scout", "succeeded", "sub-agent")
            done.updated_at = Date.now() + 10
            post("/__control/jobs", {jobs: [job("watch", "running")],
                                     event: {type: "background-job-updated", job_id: "scout", status: "succeeded", job: done}})
            stage = 2
        } else if (stage === 2 && rachel(subAgentCountRole) === 0) {
            check(rachel(backgroundJobCountRole) === 1, "a finished job leaves one process")
            check(app.processRevision > revision, "processRevision advances")
            check(app.agentProcesses("rachel").jobs.length === 1, "popover follows")
            check(Object.keys(app.agentProcesses("nobody")).length === 0, "unknown session has no processes")
            ticker.stop()
            finish()
        }
    }
}
