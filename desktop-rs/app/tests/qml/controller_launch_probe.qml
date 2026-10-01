// needs: fake-host
// Ports of tst_native_core idleContactStartsFreshWithSavedDefaults,
// idleContactUsesDialogLaunchValues, launchPoolCarriesBackendModelAndHandlesEmpty,
// contactCreateShowsHostMessageWithoutHttpSuffix,
// hostDefaultDirectoryReplacesHomePlaceholder and the session-only path of
// newAgentWaitsForOwnRosterAndRejectsLateSnapshots, plus agent mutations.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var created: []
    property int poolEmpty: 0
    property int snapshotsBefore: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function requests(path, method) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
            .filter(r => r.path === path && (!method || r.method === method))
    }
    function control(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    AppController {
        id: app
        onAgentMutationSucceeded: session => created.push(session)
        onLaunchPoolEmpty: poolEmpty++
    }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 800) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2 && app.backendOptions.length > 0) {
            check(app.lastWorkingDirectory === "/tmp" && app.launchDirectory() === "/tmp", "the Host default replaces the ~ placeholder")
            check(app.backendOptions[0].id === "claude", "catalog fallback backends")
            check(app.matchingContacts("pau").length === 1, "idle contacts match by name")
            check(!app.quickStartContact("Rachel", "", "", ""), "an active persona is not an idle contact")
            check(app.quickStartContact("paula", "grok", "grok-4", "high"), "quick start an idle contact")
            check(app.startingContact === "Paula", "startingContact names it")
            check(!app.quickStartContact("Paula", "", "", ""), "a second start waits for the first")
            stage = 1
        } else if (stage === 1 && created.length === 1) {
            const post = requests("/agents", "POST")
            check(post.length === 1, "one POST /agents")
            const body = post[0].body
            check(body.name === "Paula" && body.cwd === "/tmp" && body.backend === "grok" && body.model === "grok-4" && body.effort === "high", "launch values carried")
            check(!("session" in body) && !("replace_sid" in body) && !("resume_session_id" in body), "a fresh start carries no session ids")
            check(app.selectedSession === "paula-new" && app.panes.activeSession === "paula-new", "the new agent opens")
            check(app.composerFocusPane === app.panes.activePaneId, "its composer takes focus")
            check(app.startingContact === "" && app.lastBackend === "grok", "launch finished; backend remembered")

            control("/__control/create", {respond: {status: 409, body: {error: "contact_pool_empty"}}})
            check(app.startAvailableContact("codex", "", ""), "pool launch accepted")
            stage = 2
        } else if (stage === 2 && poolEmpty === 1) {
            check(app.startingContact === "", "an empty pool clears the launch")
            const pool = requests("/agents", "POST")[1].body
            check(pool.auto_contact === true && !("name" in pool) && pool.backend === "codex", "pool request shape")
            control("/__control/create", {respond: {status: 403, body: {error: "workspace_path_forbidden", message: "path /home/clarp is outside the Clarp workspace root /data/workspace"}}})
            app.setLaunchDirectory("/home/clarp")
            app.startAnonymousAgent("codex", "", "")
            stage = 3
        } else if (stage === 3 && app.errorMessage !== "") {
            check(app.errorMessage === "path /home/clarp is outside the Clarp workspace root /data/workspace", "Host message shown without an HTTP suffix: " + app.errorMessage)
            check(app.startingContact === "", "failed launch cleared")
            app.setLaunchDirectory("/tmp")
            control("/__control/create", {mode: "session-only"})
            snapshotsBefore = requests("/agents/snapshot").length
            check(app.startAnonymousAgent("codex", "", ""), "session-only launch accepted")
            stage = 4
        } else if (stage === 4 && requests("/agents/snapshot").length > snapshotsBefore + 1 && app.startingContact !== "") {
            // The create reply arrived (a snapshot followed it) but the roster
            // does not show the new agent yet: keep waiting.
            check(created.length === 1, "no success before the roster shows the agent")
            check(app.startAnonymousAgent("codex", "", ""), "a second start retries instead of creating")
            stage = 5
            ticks = 0
        } else if (stage === 5 && ticks > 20) {
            check(requests("/agents", "POST").length === 4, "still exactly one session-only creation")
            control("/__control/publish-pending", {})
            app.refreshAgents()
            stage = 6
        } else if (stage === 6 && created.length === 2) {
            check(created[1] === "anon-new" && app.selectedSession === "anon-new", "opens once the roster shows it")
            check(app.agents.count === 4, "roster has the new agents")
            app.renameAgent("anon-new", "  Nova  ")
            app.releaseAgent("paula-new")
            stage = 7
        } else if (stage === 7 && created.length === 4) {
            check(requests("/agent-rename", "POST")[0].body.name === "Nova", "rename trims")
            check(requests("/agents/paula-new", "DELETE").length === 1, "release deletes the agent")
            ticker.stop()
            finish()
        }
    }
}
