// needs: fake-host
// env: CLARP_STALL_THRESHOLD_MS=150
// env: CLARP_STALL_MEMORY_MB=1
// env: CLARP_STALL_LOG=$SCRATCH/stalls.log
// env: CLARP_MEMORY_LOG_SECONDS=1
// JIT code has no unwind information; interpreted, the block is in Qt's frames.
// env: QV4_FORCE_INTERPRETER=1
// Diagnostics: blocking the GUI thread writes its stack to the stall log
// and then how long it lasted; crossing the memory mark captures the stack
// too; the minute line (here each second) reports memory, items, the
// controller's caches and the stall.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int shortBlocks: 0
    property int nextBlockTick: 0
    readonly property string stallLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17).replace(/host\.log$/, "stalls.log")
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function log() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + stallLog, false)
        xhr.send()
        return xhr.responseText
    }
    AppController { id: app }
    Diagnostics {
        id: diagnostics
        onMemorySampleRequested: logMemory(7, 2)
    }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && diagnostics.watching && ticks >= nextBlockTick) {
            // Short blocks at arbitrary points between heartbeats are not
            // stalls: each is measured from the wake that started it.
            if (shortBlocks < 6) {
                const until = Date.now() + 110
                while (Date.now() < until) {}
                shortBlocks++
                nextBlockTick = ticks + 7 + shortBlocks % 3
                return
            }
            stage = 1
            // Hold the GUI thread for 400 ms.
            const until = Date.now() + 400
            while (Date.now() < until) {}
        } else if (stage === 1 && log().indexOf("-- stall ended after") >= 0) {
            const text = log()
            const stall = text.substring(text.indexOf("== stall at"))
            check(/^== stall at \d{4}-\d\d-\d\dT[\d:.]+ pid=\d+ blocked>=\d+ms build=/.test(stall), "stall header: " + stall.split("\n")[0])
            const frames = stall.split("\n").filter(l => /^#\d+ +0x[0-9a-f]+/.test(l))
            check(frames.length >= 5, frames.length + " GUI frames captured")
            check(frames.some(l => /Qml|QV4|qml/.test(l)), "the stack is the blocked script's: " + frames.slice(0, 3).join(" | "))
            const ended = Number(/-- stall ended after (\d+) ms/.exec(text)[1])
            check(ended >= 250 && ended < 2000, "stall length " + ended + " ms")
            stage = 2
        } else if (stage === 2 && diagnostics.lastMemoryReport !== "" && JSON.parse(diagnostics.lastMemoryReport).stalls >= 1
                   && log().indexOf("== memory at") >= 0) {
            const report = JSON.parse(diagnostics.lastMemoryReport)
            check(/== memory at .* rss=\d+MB build=\S+\n#0 /.test(log()), "crossing the memory mark captures the stack: " + log().split("\n").filter(l => l.startsWith("==")).join(" / "))
            check(report.VmRSS > 0 && report.RssAnon > 0 && report.VmHWM >= report.VmRSS, "kernel memory split")
            check(report.items === 7 && report.textItems === 2, "window item counts")
            check(report.conversations !== undefined && report.agents !== undefined && report.styledMarkdownCacheBytes !== undefined
                  && report.narratorCache !== undefined && report.avatarRequests !== undefined, "controller counters: " + diagnostics.lastMemoryReport)
            check(report.stalls === 1 && log().split("== stall").length === 2 && report.longestStallMs >= 250 && typeof report.cpuMsPerSec === "number", "stalls and CPU in the line")
            ticker.stop()
            finish()
        }
    }
}
