// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// C++ VoiceViewportSmokeCheck (CLARP_SCREENSHOT_VOICE_VIEWPORT) on the real
// Main: while someone reads back in a long chat, voice failures (another
// chat's, then this chat's) and a newly streamed reply arrive through the
// Host's live events. The row they are reading stays where it is, and
// another chat's failure never shows here or as the window's error.
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
    function row(id) { return find(o => o.visible && o.messageId === id) }
    function sceneY(item) { return item ? item.mapToItem(null, 0, 0).y : -99999 }
    function event(body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", ctl.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    WindowCapture { id: capture }
    property var ctl: null
    property var transcript: null
    property string anchor: ""
    property real anchorY: 0
    property int stable: 0
    property int step: 0
    property int waited: 0
    function steady(what) {
        const y = sceneY(row(anchor))
        check(Math.abs(y - anchorY) < 2, what + " (anchor " + anchor + " moved " + Math.round(y - anchorY) + " px)")
    }
    Timer {
        interval: 150; repeat: true; running: true
        onTriggered: {
            if (!ctl) { ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined); return }
            const current = ctl.conversationForSession("rachel"), other = ctl.conversationForSession("mike")
            if (++waited > 80) { check(false, "timed out at step " + step); stop(); finish(); return }
            switch (step) {
            case 0: {
                if (!ctl.connected || ctl.selectedSession !== "rachel" || current.count === 0) return
                const turns = []
                for (let i = 0; i < 80; ++i) turns.push({id: "voice-row-" + i, role: "assistant", timestamp: "2026-09-11T12:00:00Z",
                    text: "Retained fixture message " + i + ". Read this conversation without moving the viewport."})
                current.applyLogText(JSON.stringify({conversation_id: current.conversationId, turns: turns, latest_revision: 80}), "replace")
                step = 1; waited = 0
                return
            }
            case 1:
                transcript = find(o => o.objectName === "transcriptList" && o.visible)
                if (!transcript || transcript.count < 80) return
                transcript.pauseFollowing()
                transcript.contentY = transcript.originY + 900
                step = 2; waited = 0
                return
            case 2: {
                // Delegates are created and measured lazily: settle on a row in
                // the middle of the viewport first.
                if (anchor === "") {
                    const top = sceneY(transcript)
                    for (let i = 0; i < 80 && anchor === ""; ++i) {
                        const r = row("voice-row-" + i)
                        if (r && sceneY(r) > top + 120 && sceneY(r) < top + transcript.height - 100) anchor = r.messageId
                    }
                    return
                }
                const y = sceneY(row(anchor))
                stable = Math.abs(y - anchorY) < 0.5 ? stable + 1 : 0
                anchorY = y
                if (stable < 3) return
                check(ctl.errorMessage === "", "no window error before")
                event({type: "tts-error", session: "mike", message: "Voice synthesis failed.", error: "fixture provider refused synthesis"})
                step = 3; waited = 0
                return
            }
            case 3:
                if (other.voiceError === "") return
                steady("another chat's voice failure leaves the reader where they are")
                check(ctl.errorMessage === "" && current.voiceError === "", "and shows neither here nor as the window's error")
                event({type: "tts-error", session: "rachel", message: "Voice synthesis failed.", error: "fixture provider refused synthesis"})
                step = 4; waited = 0
                return
            case 4:
                if (current.voiceError === "") return
                if (waited < 3) return  // let the voice error overlay lay out
                steady("this chat's voice failure does not move the transcript either")
                current.applyLogText(JSON.stringify({conversation_id: current.conversationId, latest_revision: 81, turns: [
                    {id: "voice-stream", role: "assistant", kind: "live", text: "New spoken reply is streaming.", timestamp: "2026-09-11T12:05:00Z", revision: 81}]}), "delta")
                step = 5; waited = 0
                return
            default:
                if (current.indexOfMessage("voice-stream") < 0 || waited < 3) return
                steady("a reply streaming below does not pull the reader down")
                check(capture.capture(root, dir + "voice-viewport.png"), "the reader's view is captured")
                stop()
                finish()
            }
        }
    }
}
