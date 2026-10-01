// needs: fake-host
// C++ ReadyReplySmokeCheck (CLARP_SCREENSHOT_READY_REPLY) on the real Main:
// with "show when ready" on, a live reply shows a typing indicator and its
// tool card but never its provisional text; the finished answer replaces
// the indicator. The typing state is saved as a screenshot.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1100; height: 700
    property int failures: 0
    readonly property string dir: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17).replace(/host\.log$/, "")
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function walk(o, visit, seen) {
        if (!o || seen.has(o)) return
        seen.add(o)
        visit(o)
        for (const list of [o.data, o.children, o.resources]) {
            if (!list) continue
            for (let i = 0; i < list.length; ++i) walk(list[i], visit, seen)
        }
    }
    function find(test) {
        let found = null
        walk(root.contentItem, o => { if (!found && test(o)) found = o }, new Set())
        if (!found) walk(root, o => { if (!found && test(o)) found = o }, new Set())
        return found
    }
    // What the transcript shows now.
    function shown() {
        const state = {typing: false, partial: false, final: false, tool: false}
        walk(root.contentItem, o => {
            if (o.objectName === "replyTypingIndicator" && o.visible) state.typing = true
            if (o.objectName === "toolCard" && o.visible && o.summary === "Search project files") state.tool = true
            if (o.objectName === "messageTextBlock") {
                state.partial = state.partial || String(o.text).indexOf("Provisional secret reply") >= 0
                state.final = state.final || String(o.text).indexOf("Finished answer") >= 0
            }
        }, new Set())
        return state
    }
    WindowCapture { id: capture }
    property var ctl: null
    property var model: null
    property int step: 0
    property int waited: 0
    function update(kind, text, revision) {
        model.applyLogText(JSON.stringify({conversation_id: model.conversationId, latest_revision: revision,
            turns: [{id: "ready-reply", role: "assistant", kind: kind, text: text, revision: revision,
                     tools: [{name: "Bash", summary: "Search project files"}]}]}), "delta")
    }
    function state(kind, ts) {
        ctl.agents.applyEventText("state", JSON.stringify({session: ctl.selectedSession, kind: kind, ts: ts}))
    }
    Timer {
        interval: 140; repeat: true; running: true
        onTriggered: {
            if (!ctl) { ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined); return }
            if (!ctl.connected || ctl.selectedSession === "" || (step === 0 && ++waited < 10)) return
            model = ctl.conversationForSession(ctl.selectedSession)
            const now = shown()
            switch (step) {
            case 0:
                ctl.activityDisplayMode = 2
                ctl.showWhenReady = true
                model.applyLogText(JSON.stringify({conversation_id: model.conversationId, latest_revision: 999, turns: [
                    {id: "old-tools-a", role: "assistant", timestamp: "2020-01-01T00:00:00Z", text: "", activity_count: 3, revision: 998},
                    {id: "old-tools-b", role: "assistant", timestamp: "2020-01-01T01:32:23Z", text: "", activity_count: 2, revision: 999}]}), "delta")
                update("live", "Provisional secret reply", 1000)
                state("thinking", 4000000000000)
                step = 1; waited = 0
                return
            case 1:
                if (!(now.typing && now.tool) && ++waited < 20) return
                check(now.typing, "a live reply shows the typing indicator")
                check(!now.partial, "but not its provisional text")
                check(now.tool, "its tool card shows")
                check(model.indexOfMessage("ready-reply") >= 0, "the row exists")
                check(capture.capture(root, dir + "ready-reply.typing.png"), "the typing state is captured")
                update("assistant", "Finished answer", 1001)
                state("done", 4000000000001)
                step = 2; waited = 0
                return
            case 2:
                if (!(now.final && !now.typing) && ++waited < 20) return
                check(!now.typing && !now.partial && now.final && now.tool, "the finished answer replaces the indicator: " + JSON.stringify(now))
                // The same through the Host's live state event.
                const xhr = new XMLHttpRequest()
                xhr.open("POST", ctl.baseUrl + "/__control/event", false)
                xhr.setRequestHeader("Content-Type", "application/json")
                xhr.send(JSON.stringify({type: "agent-state", session: ctl.selectedSession, kind: "tool"}))
                step = 3; waited = 0
                return
            default:
                if (!now.typing && ++waited < 20) return
                check(now.typing, "a live agent-state event from the Host shows the indicator")
                stop()
                finish()
            }
        }
    }
}
