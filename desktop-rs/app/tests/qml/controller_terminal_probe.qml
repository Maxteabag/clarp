// needs: fake-host
// fixture-dir: project with spaces
// fixture-exec: xdg-terminal-exec #!/bin/sh\nprintf '%s\\n' "$PWD" "$@" > "$CLARP_TEST_TERMINAL_CAPTURE"\n
// fixture-exec: claude #!/bin/sh\nexit 0\n
// fixture-exec: codex #!/bin/sh\nexit 0\n
// fixture-exec: agy #!/bin/sh\nexit 0\n
// fixture-exec: grok #!/bin/sh\nexit 0\n
// env: PATH=$SCRATCH:/usr/bin:/bin
// env: CLARP_TEST_TERMINAL_CAPTURE=$SCRATCH/arguments
// C++ agentTerminalLaunchesNativeCliThroughDefaultTerminal: each backend's
// CLI opens in the default terminal in the agent's directory, resuming its
// conversation, without the Host token; not without a shared filesystem.
// Also: links, reports and copies go through the guarded desktop actions.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int backendIndex: 0
    readonly property var backends: ["claude", "codex", "agy", "grok"]
    readonly property string scratch: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17, arg.lastIndexOf("/"))
        return ""
    }
    readonly property string workspace: scratch + "/project with spaces"
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function read(name) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + scratch + "/" + name, false)
        xhr.send()
        return xhr.status === 200 || xhr.status === 0 ? xhr.responseText : ""
    }
    function control(body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/agent", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    function setBackend() {
        control({"session": "rachel", "set": {"backend": backends[backendIndex], "cwd": workspace, "conversation_id": "native-session;literal"}})
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 800) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        const backend = backends[backendIndex]
        if (stage === 0 && app.connected && app.agents.count === 2) {
            app.sharedFilesystem = true
            setBackend()
            stage = 1
        } else if (stage === 1 && app.agentDetails("rachel").backend === backend && app.agentDetails("rachel").working_directory === workspace) {
            app.openAgentTerminal("rachel")
            stage = 2
        } else if (stage === 2 && read("arguments").length > 0) {
            const lines = read("arguments").split("\n").filter(l => l.length)
            const flag = backend === "codex" ? "resume" : backend === "agy" ? "--conversation" : "--resume"
            const expected = [workspace, "--dir=" + workspace, "--title=Rachel — " + backend, "--",
                              "env", "-u", "CLARP_TOKEN", "CLAUDE_PWA_SESSION=rachel", scratch + "/" + backend, flag, "native-session;literal"]
            check(JSON.stringify(lines) === JSON.stringify(expected), backend + " opens its CLI: " + JSON.stringify(lines))
            // The next run must not see this one's capture.
            const xhr = new XMLHttpRequest()
            xhr.open("PUT", "file://" + scratch + "/arguments", false)
            xhr.send("")
            if (++backendIndex < backends.length) {
                setBackend()
                stage = 1
            } else {
                app.sharedFilesystem = false
                app.openAgentTerminal("rachel")
                stage = 3
                ticks = 0
            }
        } else if (stage === 3 && app.errorMessage !== "") {
            check(app.errorMessage.indexOf("share the local filesystem") >= 0 && read("arguments") === "", "no terminal without a shared filesystem")
            check(app.openExternalLink("https://example.com/page", "") && read("opened-urls").trim() === "https://example.com/page", "a web link opens")
            check(!app.openExternalLink("javascript:alert(1)", "") && app.errorMessage.indexOf("Only web and mail links") === 0, "other schemes are refused")
            check(!app.openExternalLink(workspace + "/nothing.html", app.baseUrl), "a local report needs the shared filesystem")
            app.copyToClipboard("copied text")
            check(read("clipboard") === "copied text", "copy goes to the (test) clipboard")
            check(String(app.resourceUrl("/media/x.png")) === app.baseUrl + "/media/x.png", "resource URLs resolve on the Host")
            app.openAgentFiles("rachel")
            check(read("opened-urls").indexOf("/project%20with%20spaces") >= 0, "the agent's files open: " + read("opened-urls"))
            ticker.stop()
            finish()
        }
    }
}
