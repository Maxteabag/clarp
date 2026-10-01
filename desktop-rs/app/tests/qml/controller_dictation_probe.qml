// needs: fake-host
// env: CLARP_AUDIO_INPUT=file:$FIXTURES/dictation.wav
// env: CLARP_AUDIO_OUTPUT=null
// Dictation: record for a chat, move focus elsewhere, stop; the WAV goes to
// /transcribe and the text is sent to the chat it was recorded for, carrying
// the trace and transcription ids.
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
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function requests(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + hostLog, false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path && r.method === "POST")
    }
    AppController { id: app }
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
            app.toggleRecordingForSession("mike")
            check(app.audio.recording, "recording")
            app.selectSession("rachel")
            stage = 1
        } else if (stage === 1 && app.selectedSession === "rachel") {
            app.toggleRecordingForSession("rachel")
            check(!app.audio.recording, "stopped")
            check(app.audio.transcriptionsForSession("mike") === 1 && app.audio.transcribing, "transcribing for mike")
            stage = 2
        } else if (stage === 2 && requests("/send").length === 1) {
            const upload = requests("/transcribe")[0].body
            check(upload.riff === "RIFF" && upload.content_type === "audio/wav" && upload.size > 16000 && upload.hands_free === "0", "a WAV was uploaded: " + JSON.stringify(upload))
            const sent = requests("/send")[0].body
            check(sent.session === "mike", "sent to the chat it was recorded for: " + sent.session)
            check(sent.text === "dictated words" && sent.trace_id === "trace-dictation", "text and trace id")
            check(sent.transcription_id === upload.transcription_id && sent.transcription_id.length > 0, "transcription id carried")
            check(!app.audio.transcribing, "nothing left in flight")
            ticker.stop()
            finish()
        }
    }
}
