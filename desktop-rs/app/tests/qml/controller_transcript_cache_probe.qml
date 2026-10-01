// needs: fake-host
// A chat's durable rows are cached on disk: with the Host unreachable, a
// fresh controller opens rachel's transcript from the cache; a chat never
// opened has nothing cached.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    id: root
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int loadedAt: 0
    property var second: null
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function ids(model) {
        const out = []
        for (let i = 0; i < model.count; ++i) out.push(model.data(model.index(i, 0), 257))
        return out
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 400) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            if (stage === 0 && app.conversation && app.conversation.count === 2) {
                loadedAt = ticks
                stage = 1
            } else if (stage === 1 && ticks > loadedAt + 20) {
                // Past the 250 ms save delay: take the Host away.
                const xhr = new XMLHttpRequest()
                xhr.open("POST", app.baseUrl + "/__control/outage", false)
                xhr.setRequestHeader("Content-Type", "application/json")
                xhr.send(JSON.stringify({"seconds": 30}))
                second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
                const rachel = second.conversationForSession("rachel")
                check(rachel.count === 2 && ids(rachel).join() === "r1,r2", "rachel opens from the cache: " + ids(rachel))
                check(second.conversationForSession("mike").count === 0, "a chat never opened has nothing cached")
                check(!second.connected, "while the Host is unreachable")
                ticker.stop()
                finish()
            }
        }
    }
}
