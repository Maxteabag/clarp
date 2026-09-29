// needs: fake-host
// env: CLARP_RESTORE_DESKTOP=1
// env: CLARP_RESTORE_SESSION=mike
// env: CLARP_AUDIO_OUTPUT=null
// A relaunched window reopens the chat it had, before and after the roster
// arrives, instead of the first agent in the sidebar.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int ticks: 0
    property bool early: false
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    AppController { id: app; Component.onCompleted: early = selectedSession === "mike" }
    Timer {
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out"); done(); return }
            if (app.agents.count === 2 && app.conversation && app.conversation.count === 2 && ticks > 20) {
                check(early, "restored before the Host answered")
                check(app.selectedSession === "mike" && app.selectedName === "Mike", "still mike once the roster arrived, not the first row")
                check(app.errorMessage === "", "no error: " + app.errorMessage)
                done()
            }
        }
    }
    function done() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
}
