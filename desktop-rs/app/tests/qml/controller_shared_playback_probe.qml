// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// C++ sharedPlaybackDoesNotDuplicateDownloads: two windows on one Host and
// account elect one player over the session bus. A clip both receive is
// downloaded and played once, a clip that finished is never replayed, and
// muting in one window mutes both.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    id: root
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var second: null
    property int firstMuted: 0
    property int secondMuted: 0
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
    function log() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
    }
    function downloads() { return log().filter(r => r.path === "/clips/1/complete.mp3").length }
    function acks() { return log().filter(r => r.path === "/clips/ack").map(r => r.body.status).sort().join() }
    function announce() {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify({"type": "audio", "clip_id": 1, "session": "rachel",
                                 "url": "/clips/1/complete.mp3", "complete_url": "/clips/1/complete.mp3"}))
    }
    AppController { id: app; onMutedChanged: if (muted) firstMuted++ }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 800) { check(false, "timed out at stage " + stage + " acks " + acks() + " downloads " + downloads()); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected) {
            second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
            second.mutedChanged.connect(() => { if (second.muted) secondMuted++ })
            stage = 1
        } else if (stage === 1 && second.connected && ticks > 80) {
            // Both windows are on the bus by now; one owns playback.
            announce()
            stage = 2
        } else if (stage === 2 && acks().indexOf("play-ok") >= 0) {
            stage = 3
            ticks = 0
        } else if (stage === 3 && ticks > 40) {
            check(downloads() === 1, "downloaded once: " + downloads())
            check(acks() === "play-ok,play-start,queued", "played once: " + acks())
            announce()
            stage = 4
            ticks = 0
        } else if (stage === 4 && ticks > 60) {
            check(downloads() === 1 && acks() === "play-ok,play-start,queued", "a finished clip is never replayed: " + acks())
            second.muted = true
            stage = 5
        } else if (stage === 5 && app.muted && second.muted) {
            check(firstMuted === 1 && secondMuted === 1, "muting one window mutes both: " + firstMuted + "/" + secondMuted)
            ticker.stop()
            finish()
        }
    }
}
