// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=decode
// The real decoders without a device: mp3 and mp4/aac clips decode and are
// acknowledged as played, raw PCM plays, and bytes that are not audio fail
// the clip with a decode error instead of stalling the queue.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function acks(clip) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
            .filter(r => r.path === "/clips/ack" && r.body.clip_id === clip).map(r => r.body)
    }
    function announce(body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + "/__control/event", false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(Object.assign({"type": "audio", "session": "rachel"}, body)))
    }
    // Acks are separate requests and may reach the Host out of order.
    function statuses(clip) { return acks(clip).map(a => a.status).sort().join() }
    function done(clip) { return acks(clip).some(a => a.status === "play-ok" || a.status === "play-fail") }
    AppController { id: app }
    Timer {
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); finish(); return }
            if (stage === 0 && app.connected) {
                announce({"clip_id": 11, "url": "/fixtures/clip.mp3", "complete_url": "/fixtures/clip.mp3"})
                announce({"clip_id": 12, "url": "/fixtures/clip.m4a", "complete_url": "/fixtures/clip.m4a"})
                announce({"clip_id": 13, "url": "/fixtures/tone.pcm", "stream_url": "/fixtures/tone.pcm",
                          "audio_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000, "channels": 1}})
                announce({"clip_id": 14, "url": "/clips/14/complete.mp3", "complete_url": "/clips/14/complete.mp3"})
                stage = 1
            } else if (stage === 1 && [11, 12, 13, 14].every(done)) {
                for (const clip of [11, 12, 13])
                    check(statuses(clip) === "play-ok,play-start,queued", "clip " + clip + " decoded and played: " + statuses(clip))
                const bad = acks(14).find(a => a.status === "play-fail")
                check(bad.status === "play-fail" && bad.error.indexOf("could not be decoded") >= 0, "not audio fails the clip: " + bad.error)
                finish()
            }
        }
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
}
