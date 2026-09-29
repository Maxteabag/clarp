// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// Voice clips through the silent test sink: an announced clip is queued,
// downloaded, played and acknowledged; selecting a chat plays its
// recoverable clips; silence interrupts; mute reaches /send and persists.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property bool sawPlaying: false
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
    function requests(path, method) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l))
            .filter(r => r.path === path && (!method || r.method === method))
    }
    function acks(clip) { return requests("/clips/ack", "POST").filter(r => r.body.clip_id === clip).map(r => r.body.status) }
    function control(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    AppController { id: app }
    Connections {
        target: app.audio
        function onPlayingChanged() { if (app.audio.playing) sawPlaying = true }
    }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            check(app.audio !== null && !app.audio.playing && !app.audio.recording, "audio idle")
            control("/__control/event", {"type": "audio", "clip_id": 1, "session": "rachel", "trace_id": "t1",
                                         "url": "/clips/1/complete.mp3", "complete_url": "/clips/1/complete.mp3"})
            stage = 1
        } else if (stage === 1 && acks(1).indexOf("play-ok") >= 0) {
            check(JSON.stringify(acks(1)) === JSON.stringify(["queued", "play-start", "play-ok"]), "acknowledged in order: " + acks(1))
            check(sawPlaying && !app.audio.playing, "played and stopped")
            check(requests("/clips/1/complete.mp3", "GET").length === 1, "downloaded once")
            app.selectSession("mike")
            stage = 2
        } else if (stage === 2 && acks(50).indexOf("play-ok") >= 0) {
            check(requests("/clips/recoverable", "GET").some(r => r.query.session === "mike"), "selecting asks for recoverable clips")
            control("/__control/event", {"type": "audio", "clip_id": 2, "session": "mike", "url": "/clips/2/complete.mp3"})
            stage = 3
        } else if (stage === 3 && acks(2).indexOf("play-start") >= 0) {
            app.audio.silence()
            stage = 4
        } else if (stage === 4 && acks(2).length === 3) {
            const last = requests("/clips/ack", "POST").filter(r => r.body.clip_id === 2).pop().body
            check(last.status === "play-fail" && last.error === "interrupted by user", "silence interrupts the clip")
            app.muted = true
            app.sendMessage("mike", "quiet please")
            stage = 5
        } else if (stage === 5 && requests("/send", "POST").length === 1) {
            check(requests("/send", "POST")[0].body.synthesize_audio === false, "muted sends ask for no speech")
            control("/__control/event", {"type": "audio", "clip_id": 3, "session": "mike", "url": "/clips/3/complete.mp3"})
            stage = 6
            sawPlaying = false
        } else if (stage === 6 && ticks > 200) {
            check(acks(3).length === 0 && !sawPlaying, "a muted window plays nothing")
            app.muted = false
            app.sendMessage("mike", "speak")
            stage = 7
        } else if (stage === 7 && requests("/send", "POST").length === 2) {
            check(requests("/send", "POST")[1].body.synthesize_audio === true, "unmuted sends ask for speech")
            app.audio.startRecording()
            stage = 8
        } else if (stage === 8 && app.errorMessage !== "") {
            // Reaches the controller through a queued connection.
            check(!app.audio.recording && app.errorMessage === "No microphone is available", "no microphone yet: " + app.errorMessage)
            ticker.stop()
            finish()
        }
    }
}
