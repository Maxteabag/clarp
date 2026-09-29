// needs: fake-host
// env: CLARP_SETTINGS=$SCRATCH/settings.json
// Controller preferences persist under the C++ keys and restore in the next
// controller; reading styles resolve fonts; small queries match the C++.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    id: root
    width: 200; height: 200; visible: true
    property int failures: 0
    property int ticks: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out"); ticker.stop(); finish(); return }
            if (!app.connected || app.agents.count !== 2) return
            ticker.stop()
            try { run() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack) }
            finish()
        }
    }
    function run() {
        check(app.workspaceBarVisible && !app.minimalUi && app.pauseMobilePush && app.anonymousAgents, "C++ defaults")
        check(app.readingTheme === "terminal" && app.readingThemes.length === 4, "terminal theme by default")
        check(app.readingStyle.background === "#1a1b26" && app.readingStyle.fontPixelSize === 15, "reading style palette")
        app.readingTheme = "paper"
        check(app.readingStyle.light === true && app.readingStyle.fontFamily !== "", "paper is light with a resolved font: " + app.readingStyle.fontFamily)
        app.readingTheme = "no-such-theme"
        check(app.readingTheme === "terminal", "unknown themes normalise to terminal")
        app.readingTheme = "dusk"
        app.minimalUi = true
        app.workspaceBarVisible = false
        app.timestampsVisible = true
        app.muted = true
        app.pauseMobilePush = false
        app.newAgentOnStartup = false
        app.anonymousAgents = false
        app.sharedFilesystem = true
        const next = Qt.createQmlObject('import Clarp.Native; AppController {}', root)
        check(next.readingTheme === "dusk" && next.minimalUi && !next.workspaceBarVisible && next.timestampsVisible, "appearance restored")
        check(next.muted && !next.pauseMobilePush && !next.newAgentOnStartup && !next.anonymousAgents, "behaviour restored")
        check(next.sharedFilesystem, "shared filesystem restored for the same Host")
        next.destroy()

        const details = app.agentDetails("rachel")
        check(details.name === "Rachel" && details.backend === "claude" && details.state === "idle", "agentDetails")
        check(details.workspace && details.workspace.kind === "directory", "workspace context included")
        check(Object.keys(app.agentDetails("nobody")).length === 0, "unknown agent has no details")
        check(app.matchingAgents("mik").length === 1 && app.matchingAgents("").length === 2, "matchingAgents")
        check(app.agentNameById("a2") === "Mike" && app.agentSessionById("a2") === "mike", "lookups by agent id")
        check(app.isPairSession("pair:a:b") && !app.isPairSession("rachel"), "pair rooms")
        check(app.chatStamp(0) === "" && app.chatStamp(Date.now()) !== "", "chat stamps")
        check(app.markdownDisplayBlocks("one\n\ntwo").length === 2, "markdown display blocks")
        check(app.canLinkifyOutput("see https://x.test") && !app.canLinkifyOutput("no links"), "linkify needs a URL")
        check(app.linkifiedOutput("go https://x.test").indexOf('<a href="https://x.test">') >= 0, "linkified output")
        check(app.backgroundJobProgress({metadata: {progress: 0.25}}) === 0.25, "job progress from a map")
    }
}
