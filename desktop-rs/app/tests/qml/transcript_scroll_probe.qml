// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// C++ TranscriptScrollSmokeCheck phase A (CLARP_SCREENSHOT_TRANSCRIPT_SCROLL)
// on the real Main: the real transcript (model -> presentation -> native
// TranscriptLayout -> MessageDelegate) follows a streaming reply at the end,
// a real wheel turn pauses it and moves the reader, and streaming, a new
// final row and a refresh never pull a paused reader away; jumping to the
// latest resumes following, as does wheeling back to the end. The fixture
// keeps the chat's own conversation id: the Host is live here (the C++ lane
// ran offline), and a different id rightly makes the controller reload.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1100; height: 700
    property int failures: 0
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
    function pad(n) { return (n < 10 ? "0" : "") + n }
    function turn(i, live) {
        const user = i % 2 === 0
        const row = {id: "fixture-" + i, role: user ? "user" : "assistant", timestamp: "2026-09-15T08:" + pad(i % 60) + ":00Z", revision: i + 1}
        if (user) {
            row.text = "Question " + i + ": what changed in the transcript scroll code?"
        } else {
            row.text = live ? "Streaming answer" : "Answer " + i + ". The list follows new rows while the reader is at the end and stays put otherwise.\n\nSecond paragraph with a little more text so rows differ in height."
            if (live) row.kind = "live"
            row.tools = [{name: "Read", summary: "desktop/qml/components/TranscriptList.qml"}]
        }
        return row
    }
    function turns(count, liveTail) { const rows = []; for (let i = 0; i < count; ++i) rows.push(turn(i, liveTail && i === count - 1)); return rows }
    readonly property var finalRow: ({id: "fixture-final", role: "assistant", text: "Final answer replaces the live row.", timestamp: "2026-09-15T08:41:00Z", revision: 90})
    KeyInjector { id: input }
    property var ctl: null
    property var model: null
    property var transcript: null
    property int step: 0
    property int streamed: 0
    property real readerY: 0
    function stream() {
        ++streamed
        const live = turn(39, true)
        let text = "Streaming answer"
        for (let i = 0; i < streamed; ++i) text += " and more streamed words arrive here (" + i + ")"
        live.text = text
        live.revision = 40 + streamed
        model.applyLogText(JSON.stringify({conversation_id: model.conversationId, turns: [live], latest_revision: 40 + streamed}), "delta")
    }
    function wheel(notches) {
        const centre = transcript.mapToItem(null, transcript.width / 2, transcript.height / 2)
        input.wheel(root, centre.x, centre.y, notches)
    }
    Timer {
        interval: 220; repeat: true; running: true
        onTriggered: {
            if (!ctl) { ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined); return }
            if (step === 0 && (!ctl.connected || ctl.selectedSession !== "rachel" || ctl.conversationForSession("rachel").count === 0)) return
            model = ctl.conversationForSession("rachel")
            transcript = find(o => o.objectName === "transcriptList" && o.visible && o.width > 0)
            if (!transcript) { check(false, "a visible transcript"); stop(); finish(); return }
            const contentY = transcript.contentY, distance = transcript.distanceFromBottom
            const atEnd = transcript.atLatest, following = transcript.followLatest
            const require = (ok, what) => { check(ok, what + " (contentY " + Math.round(contentY) + ", distance " + Math.round(distance) + ", following " + following + ")"); if (!ok) { stop(); finish() } return ok }
            switch (step++) {
            case 0:
                ctl.toolsVisible = true
                model.applyLogText(JSON.stringify({conversation_id: model.conversationId, turns: turns(40, true), latest_revision: 40}), "tail")
                break
            case 1:
                if (!require(atEnd && following, "the initial load ends at the bottom")) return
                stream()
                break
            case 2:
                if (!require(atEnd && following && distance < 2, "streaming while following keeps the end visible")) return
                stream()
                break
            case 3:
                if (!require(atEnd && following && distance < 2, "a second stream while following keeps the end visible")) return
                wheel(5)
                break
            case 4:
                if (!require(!following && distance > 200, "a wheel turn up pauses following and moves the reader")) return
                readerY = contentY
                stream()
                break
            case 5:
                if (!require(Math.abs(contentY - readerY) < 2 && !following, "streaming does not move a paused reader")) return
                model.applyLogText(JSON.stringify({conversation_id: model.conversationId, turns: [finalRow], latest_revision: 90}), "delta")
                break
            case 6: {
                if (!require(Math.abs(contentY - readerY) < 2 && !following, "a new final row does not move a paused reader")) return
                const refreshed = turns(39, false)
                refreshed.push(finalRow)
                model.applyLogText(JSON.stringify({conversation_id: model.conversationId, turns: refreshed, latest_revision: 90}), "tail")
                break
            }
            case 7:
                if (!require(Math.abs(contentY - readerY) < 4 && !following, "a refresh restores the paused reader's position")) return
                transcript.scrollToLatest()
                break
            case 8:
                if (!require(atEnd && following && distance < 2, "jumping to the latest reaches the bottom and resumes following")) return
                wheel(3)
                break
            case 9:
                if (!require(!following, "a second wheel turn up pauses following")) return
                wheel(-3)
                break
            case 10:
                if (!require(atEnd && following, "wheeling back to the end resumes following")) return
                stream()
                break
            default:
                if (!require(atEnd && following && distance < 2, "streaming after resuming keeps the end visible")) return
                stop()
                finish()
            }
        }
    }
}
