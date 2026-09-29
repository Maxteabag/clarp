// needs: fake-host
// env: CLARP_AUDIO_INPUT=file:$FIXTURES/click.wav
// A recording too short to be speech is dropped with a message and never
// sent to /transcribe.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int ticks: 0
    property int stage: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function transcribes() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === "/transcribe").length
    }
    AppController { id: app }
    Timer {
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); done(); return }
            if (stage === 0 && app.connected) {
                app.toggleRecordingForSession("rachel")
                app.toggleRecordingForSession("rachel")
                stage = 1
            } else if (stage === 1 && app.errorMessage !== "") {
                check(app.errorMessage === "Recording was too short", "too short: " + app.errorMessage)
                check(!app.audio.recording && !app.audio.transcribing && transcribes() === 0, "nothing uploaded")
                done()
            }
        }
    }
    function done() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
}
