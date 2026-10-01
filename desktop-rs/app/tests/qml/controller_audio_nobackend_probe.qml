// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=none
// C++ clipFailsFastWithoutMediaBackend: without a playback backend both
// clips are queued and failed instead of the first blocking the queue, and
// the missing backend is reported once, not per clip.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int errors: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function acks() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === "/clips/ack").map(r => r.body)
    }
    function control(body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    AppController { id: app }
    Connections {
        target: app.audio
        function onMediaError(message) { errors++ }
    }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            if (stage === 0 && app.connected) {
                const clip = {"type": "audio", "session": "rachel", "url": "/clips/1/complete.mp3", "complete_url": "/clips/1/complete.mp3",
                              "audio_format": {"container": "mp3"}}
                control(Object.assign({"clip_id": 1}, clip))
                control(Object.assign({"clip_id": 2}, clip))
                stage = 1
            } else if (stage === 1 && acks().length >= 4) {
                const all = acks()
                check(all.length === 4 && all[3].status === "play-fail", "both clips queued then failed: " + all.map(a => a.clip_id + ":" + a.status))
                check(!app.audio.playing, "not playing")
                stage = 2
            } else if (stage === 2 && ticks > stage * 0 + 200) {
                check(errors === 1, "reported once, not per clip: " + errors)
                ticker.stop()
                console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
                Qt.exit(failures === 0 ? 0 : 1)
            }
        }
    }
}
