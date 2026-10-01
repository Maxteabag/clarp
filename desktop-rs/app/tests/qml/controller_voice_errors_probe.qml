// needs: fake-host
// env: CLARP_AUDIO_OUTPUT=null
// tst_native_core::voiceErrorsStayInTheirSession: a voice failure shows in
// its own chat, never another's or the window's; dismissing one leaves the
// others; the chat's next clip clears it; an unscoped one is the window's.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var current: null
    property var other: null
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function control(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected) {
            current = app.conversationForSession("rachel")
            other = app.conversationForSession("mike")
            control("/__control/event", {"type": "tts-error", "session": "mike", "message": "Other voice failed"})
            stage = 1
        } else if (stage === 1 && other.voiceError === "Other voice failed") {
            check(current.voiceError === "" && app.errorMessage === "", "another chat's voice error stays there")
            control("/__control/event", {"type": "tts-error", "session": "rachel", "message": "Current voice failed"})
            stage = 2
        } else if (stage === 2 && current.voiceError === "Current voice failed") {
            check(app.errorMessage === "" && current.error === "", "a voice error is not a chat or window error")
            current.voiceError = ""
            check(current.voiceError === "" && other.voiceError === "Other voice failed", "dismissing one leaves the other")
            control("/__control/event", {"type": "audio", "clip_id": 9, "session": "mike", "url": "/clips/1/complete.mp3"})
            stage = 3
        } else if (stage === 3 && other.voiceError === "") {
            check(true, "the chat's next clip clears its voice error")
            control("/__control/event", {"type": "tts-error", "error": "Unscoped voice failed"})
            stage = 4
        } else if (stage === 4 && app.errorMessage === "Unscoped voice failed") {
            check(true, "an unscoped voice error is the window's")
            ticker.stop()
            finish()
        }
    }
}
