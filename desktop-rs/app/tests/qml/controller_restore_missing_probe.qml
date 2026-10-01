// needs: fake-host
// env: CLARP_RESTORE_DESKTOP=1
// env: CLARP_RESTORE_SESSION=missing-restored-target
// C++ restoredSessionDoesNotFallBackToAnotherAgent: a restored chat the
// Host no longer has is reported, never swapped for another agent.
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
    AppController { id: app }
    property int stage: 0
    Timer {
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out"); done(); return }
            if (stage === 0 && app.agents.count === 2 && app.errorMessage !== "") {
                check(app.selectedSession === "missing-restored-target", "the restored target stays selected")
                check(app.errorMessage.indexOf("before updating") >= 0, "and it says why: " + app.errorMessage)
                app.selectSession("rachel")
                check(app.selectedSession === "rachel", "choosing another chat works")
                done()
            }
        }
    }
    function done() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
}
