// needs: fake-host
// Avatars (bundled portrait fetched, rounded, cached on disk; a missing one
// is not retried) and chat media (listed, images cached as files, links in
// markdown resolved) against the fake Host.
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
    property int retryTick: 0
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
        return xhr.responseText.split("\n").filter(l => l).map(l => JSON.parse(l)).filter(r => r.path === path)
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
            check(String(app.avatarSource("rachel")) === "", "no portrait before it arrives")
            check(String(app.avatarSource("mike")) === "", "mike's portrait requested")
            app.avatarSource("rachel")
            check(requests("/static/avatars/rachel.png").length <= 1, "one request per portrait")
            app.loadMedia("rachel")
            stage = 1
        } else if (stage === 1 && String(app.avatarSource("rachel")).startsWith("data:image/png;base64,")
                   && String(app.mediaSource("m1")) !== "") {
            check(app.avatarRevision > 0, "avatar revision advanced")
            check(requests("/static/avatars/rachel.png").length === 1, "portrait fetched once")
            const media = app.mediaForSession("rachel")
            check(media.length === 2 && media[0].asset_id === "m1", "media listed")
            check(String(app.mediaSource("m1")).startsWith("file://") && String(app.mediaSource("doc1")) === "", "only images are cached, as files")
            check(requests("/media/files/doc1").length === 0, "non-images are not downloaded")
            const md = app.resolveMediaMarkdown("![x](clarp-media://asset/m1) ![y](clarp-media://asset/nope)")
            check(md === "![x](" + app.mediaSource("m1") + ") ![y](clarp-media://asset/nope)", "media links resolve: " + md)
            stage = 2
        } else if (stage === 2 && requests("/static/avatars/mike.png").length === 1 && ticks > 60) {
            check(String(app.avatarSource("mike")) === "", "a missing portrait stays empty")
            retryTick = ticks
            stage = 21
        } else if (stage === 21 && ticks > retryTick + 20) {
            // Long enough for a retried request to reach the Host.
            check(requests("/static/avatars/mike.png").length === 1, "a failed portrait is not retried")
            second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
            stage = 3
        } else if (stage === 3 && second.connected && second.agents.count === 2) {
            const cached = String(second.avatarSource("rachel"))
            check(cached.startsWith("file://") && cached.endsWith(".png"), "a new controller reads the portrait cache: " + cached)
            check(requests("/static/avatars/rachel.png").length === 1, "no second download")
            ticker.stop()
            finish()
        }
    }
}
