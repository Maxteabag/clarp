// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// C++ TranscriptSwitchSmokeCheck (CLARP_SCREENSHOT_TRANSCRIPT_SWITCH) on the
// real Main: every frame painted while the pane switches chats, splits and
// moves focus is recorded. No frame may show a transcript from the wrong
// chat, a newly shown chat away from its end, or (on a focus change) either
// transcript moving; a paused reader stays paused.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1300; height: 760
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
    function ancestor(item, has) { let p = item ? item.parent : null; while (p && p[has] === undefined) p = p.parent; return p }
    // Visible transcripts by the session of the pane that shows them.
    function transcripts() {
        const result = []
        walk(root.contentItem, o => {
            if (o.objectName === "transcriptList" && o.visible && o.width > 0) {
                const pane = ancestor(o, "session")
                result.push({session: pane ? pane.session : "", item: o})
            }
        }, new Set())
        return result
    }
    function panes() {
        const result = []
        walk(root.contentItem, o => { if (o.boundConversationSession !== undefined && o.visible) result.push(o) }, new Set())
        return result
    }
    function view(session) { const t = transcripts().find(t => t.session === session); return t ? t.item : null }
    property var frames: []
    property int step: -1
    onAfterAnimating: {
        if (step < 0) return
        for (const t of transcripts())
            frames.push({step: step, session: t.session, modelSession: t.item.modelSession !== undefined ? t.item.modelSession : ancestor(t.item, "modelSession") ? ancestor(t.item, "modelSession").modelSession : "",
                         contentY: t.item.contentY, height: t.item.height, atEnd: t.item.atYEnd, follow: t.item.followLatest,
                         sceneY: t.item.mapToItem(null, 0, 0).y})
    }
    function framesFor(forStep, session) { return frames.filter(f => f.step === forStep && f.session === session) }
    function everyFrameShowsPaneSession(forStep, session) { const f = framesFor(forStep, session); return f.length > 0 && f.every(x => x.modelSession === session) }
    function everyFrameAtEnd(forStep, session) { const f = framesFor(forStep, session); return f.length > 0 && f.every(x => x.atEnd) }
    function frozen(forStep, session) {
        const f = framesFor(forStep, session)
        return f.length > 0 && f.every(x => Math.abs(x.contentY - f[0].contentY) <= 0.5 && Math.abs(x.height - f[0].height) <= 0.5 && Math.abs(x.sceneY - f[0].sceneY) <= 0.5)
    }
    function describe(forStep, session) {
        const f = framesFor(forStep, session)
        return f.length + " frames; first " + JSON.stringify(f[0] || {}) + "; bad " + JSON.stringify(f.find(x => !x.atEnd || x.modelSession !== session) || {})
    }
    function pad(n) { return (n < 10 ? "0" : "") + n }
    function load(session, count) {
        const rows = []
        for (let i = 0; i < count; ++i) {
            const user = i % 2 === 0
            rows.push({id: session + "-" + i, role: user ? "user" : "assistant", timestamp: "2026-09-15T08:" + pad(i % 60) + ":00Z", revision: i + 1,
                       text: user ? "Question " + i + " for " + session : "Answer " + i + ". Rows differ in height.\n\nSecond paragraph for " + session + "."})
        }
        ctl.conversationForSession(session).applyLogText(JSON.stringify({conversation_id: "c-" + session, turns: rows, latest_revision: count}), "replace")
    }
    function wheel(item, notches) {
        const centre = item.mapToItem(null, item.width / 2, item.height / 2)
        input.wheel(root, centre.x, centre.y, notches)
    }
    function leafId(session) {
        const leaf = ancestor(view(session), "node")
        return leaf ? leaf.node.id : ""
    }
    function require(ok, what) { check(ok, what); if (!ok) { ticker.stop(); finish() } return ok }
    KeyInjector { id: input }
    property var ctl: null
    property bool added: false
    Timer {
        id: ticker
        interval: 100; repeat: true; running: true
        onTriggered: {
            if (!ctl) { ctl = find(o => o.selectedSession !== undefined && o.agents !== undefined && o.panes !== undefined); return }
            if (step < 0) {
                if (!ctl.connected || ctl.agents.count < 2) return
                if (ctl.agents.count === 2 && !added) {
                    added = true
                    const xhr = new XMLHttpRequest()
                    xhr.open("POST", ctl.baseUrl + "/__control/add-agent", false)
                    xhr.setRequestHeader("Content-Type", "application/json")
                    xhr.send(JSON.stringify({session: "cedar", persona: "Cedar"}))
                    return
                }
                if (ctl.agents.count < 3 || ctl.conversationForSession("rachel").count === 0) return
            }
            // Advance only once this step's change has painted and every pane
            // has finished binding (a debug build takes ~300 ms to bind a
            // chat; switching again within the panes' 120 ms bind-storm
            // window is deliberately deferred and would test that instead).
            if (step >= 0) {
                const since = Math.min.apply(null, panes().map(p => Date.now() - p.lastConversationBindAt))
                if (frames.filter(f => f.step === step).length === 0 || since < 200 || panes().some(p => p.conversationBindPending)) return
            }
            ++step
            switch (step) {
            case 0:
                ctl.muted = true
                ctl.selectSession("rachel")
                load("rachel", 60); load("mike", 70); load("cedar", 50)
                ctl.clearError()
                break
            case 1: {
                const aura = view("rachel")
                if (!require(aura !== null && aura.atYEnd, "the first chat opens at its end")) return
                wheel(aura, 6)
                break
            }
            case 2:
                if (!require(view("rachel") !== null && !view("rachel").followLatest, "a wheel turn pauses it")) return
                ctl.selectSession("mike")
                break
            case 3:
                if (!require(view("mike") !== null && framesFor(2, "mike").length > 0, "the next chat shows after the switch")) return
                if (!require(everyFrameShowsPaneSession(2, "mike"), "switching chats never paints the wrong chat's transcript: " + describe(2, "mike"))) return
                if (!require(everyFrameAtEnd(2, "mike"), "nor the new chat away from its end: " + describe(2, "mike"))) return
                ctl.selectSession("rachel")
                break
            case 4:
                if (!require(view("rachel") !== null, "switching back shows the first chat")) return
                if (!require(everyFrameShowsPaneSession(3, "rachel"), "switching back never paints the wrong chat: " + describe(3, "rachel"))) return
                if (!require(everyFrameAtEnd(3, "rachel"), "and opens it at its end: " + describe(3, "rachel"))) return
                ctl.panes.splitActive("vertical", "cedar")
                break
            case 5:
                if (!require(view("cedar") !== null && view("rachel") !== null, "both panes show after the split")) return
                if (!require(everyFrameShowsPaneSession(4, "cedar") && everyFrameShowsPaneSession(4, "rachel"), "split panes never paint the wrong chat")) return
                if (!require(everyFrameAtEnd(4, "cedar"), "the new pane opens at its end without an off-end frame: " + describe(4, "cedar"))) return
                if (!require(everyFrameAtEnd(4, "rachel"), "the other pane stays at its end through the split: " + describe(4, "rachel"))) return
                wheel(view("rachel"), 4)
                break
            case 6:
                if (!require(leafId("rachel") !== "", "the first pane's leaf")) return
                ctl.panes.focusPane(leafId("rachel"))
                break
            case 7:
                if (!require(frozen(6, "rachel") && frozen(6, "cedar"), "focusing a pane moves neither transcript")) return
                ctl.panes.focusPane(leafId("cedar"))
                break
            default:
                if (!require(frozen(7, "rachel") && frozen(7, "cedar"), "focusing back moves neither transcript")) return
                if (!require(!view("rachel").followLatest, "the paused pane stays paused through focus changes")) return
                ticker.stop()
                finish()
            }
        }
    }
}
