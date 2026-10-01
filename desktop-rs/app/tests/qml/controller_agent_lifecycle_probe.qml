// needs: fake-host
// REWRITE_PLAN "Create/relaunch/fork/release agents": what each launch asks
// the Host for. A relaunch replaces its session, resume and fork name the
// past native session, MCP servers go by name, archive and release reach the
// Host, and new agents speak unless the desktop is muted (a resumed native
// session only reopens), as the C++ client sends.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int step: 0
    property int ticks: 0
    property int waitingFor: -1
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function posts(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path && r.method === "POST").map(r => r.body)
    }
    function requests(method, path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path && r.method === method)
    }
    AppController { id: app }
    // Each launch, then what its request must carry; the next runs once the
    // created agent is in the roster.
    readonly property var launches: [
        {run: () => app.startAnonymousAgent("codex", "", ""), session: "anon-new",
         expect: b => b.synthesize_audio === true && b.anonymous === true, what: "an anonymous agent speaks"},
        {run: () => app.createAgent("Nova", "/work", "claude", "", "", "rachel", "fresh", "", ["github"]), session: "nova-new",
         expect: b => b.replace_sid === "rachel" && JSON.stringify(b.mcp_servers) === '["github"]' && b.synthesize_audio === true
                      && !("resume_session_id" in b) && !("fork_session_id" in b), what: "a relaunch replaces its session with MCP servers by name"},
        {run: () => app.createAgent("Remy", "/work", "claude", "", "", "", "resume", "past-1", []), session: "remy-new",
         expect: b => b.resume_session_id === "past-1" && !("fork_session_id" in b) && !("replace_sid" in b), what: "resume names the past session"},
        {run: () => app.createAgent("Fern", "/work", "claude", "", "", "", "fork", "past-2", []), session: "fern-new",
         expect: b => b.fork_session_id === "past-2" && !("resume_session_id" in b), what: "fork names the past session"},
        {run: () => { app.muted = true; app.createAgent("Mute", "/work", "claude", "", "", "", "fresh", "", []) }, session: "mute-new",
         expect: b => b.synthesize_audio === false, what: "a muted desktop's new agent does not speak"},
        {run: () => { app.muted = false; return app.resumeLaunchSession("codex", "native-9", true) }, session: "anon-new",
         expect: b => b.resume_session_id === "native-9" && b.open_existing === true && b.synthesize_audio === false, what: "a resumed native session only reopens"},
    ]
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 800) { check(false, "timed out at step " + step); ticker.stop(); finish(); return }
            try { advance() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function advance() {
        if (!app.connected) return
        if (waitingFor >= 0) {
            const bodies = posts("/agents")
            if (bodies.length <= waitingFor || app.agents.indexOfSession(launches[step].session) < 0 || app.startingContact !== "") return
            const body = bodies[waitingFor]
            check(launches[step].expect(body), launches[step].what + ": " + JSON.stringify(body))
            waitingFor = -1
            step++
            return
        }
        if (step < launches.length) {
            waitingFor = posts("/agents").length
            launches[step].run()
            return
        }
        if (step === launches.length) {
            app.archiveAgent("mike")
            app.releaseAgent("nova-new")
            step++
            return
        }
        const archive = posts("/agent-archive")
        const released = requests("DELETE", "/agents/nova-new")
        if (archive.length === 0 || released.length === 0) return
        check(archive.length === 1 && archive[0].session === "mike" && archive[0].archived === true, "archive reaches the Host: " + JSON.stringify(archive))
        check(released.length === 1, "release deletes the agent on the Host")
        ticker.stop()
        finish()
    }
}
