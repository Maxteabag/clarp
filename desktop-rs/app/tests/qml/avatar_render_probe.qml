// needs: fake-host
// tst_avatar_render: the real AvatarActivity and ActivitySweep, driven by
// the controller's motion clock. Frames differ while an agent works, hold
// still under reduced motion or with the window hidden (the clock stops),
// and hold still once the agent is idle.
import QtQuick
import QtQuick.Window
import Clarp.Desktop
import "../../qml/components" as Product

Window {
    id: window
    width: 400; height: 130; visible: true; color: "#1a1b26"
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property int shots: 0
    property string last: ""
    readonly property string scratch: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17).replace(/host\.log$/, "")
        return ""
    }
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
    // The frame as PNG bytes, hex-encoded for comparison.
    function frame() {
        const path = scratch + "frame" + (shots++) + ".png"
        if (!capture.capture(window, path)) { check(false, "captured " + path); return "" }
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + path, false)
        xhr.responseType = "arraybuffer"
        xhr.send()
        return Array.from(new Uint8Array(xhr.response)).join(",")
    }
    AppController { id: app }
    WindowCapture { id: capture }
    Product.AvatarActivity {
        id: avatar
        x: 25; y: 40; controller: app; session: "rachel"; name: "Rachel"; working: authoritativeWorking; avatarSize: 40
    }
    Text {
        x: 90; y: 48; text: "Rachel · Working"; color: "#c7c9dc"; font.pixelSize: 14
        Product.ActivitySweep { anchors.fill: parent; working: avatar.authoritativeWorking; reducedMotion: avatar.reducedMotion; phase: avatar.phase }
    }
    Timer {
        id: ticker
        interval: 50; repeat: true; running: true
        onTriggered: {
            if (++ticks > 300) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    property int waitUntil: 0
    function step() {
        if (ticks < waitUntil) return
        const clock = app.avatarMotion
        if (stage === 0 && app.connected && app.agents.count > 0) {
            control("/__control/agent", {"session": "rachel", "set": {"latest_state": "thinking"}})
            stage = 1
        } else if (stage === 1 && avatar.authoritativeWorking) {
            check(clock.ticking, "a working agent's avatar ticks")
            last = frame(); stage = 2; waitUntil = ticks + 12
        } else if (stage === 2) {
            check(frame() !== last, "frames differ while it works")
            clock.reducedMotion = true
            stage = 3; waitUntil = ticks + 4
        } else if (stage === 3) {
            check(!clock.ticking, "reduced motion stops the clock")
            last = frame(); stage = 4; waitUntil = ticks + 10
        } else if (stage === 4) {
            check(frame() === last, "and the frame holds still")
            clock.reducedMotion = false
            window.visible = false
            stage = 5; waitUntil = ticks + 4
        } else if (stage === 5) {
            check(!clock.ticking, "a hidden window stops the clock")
            window.visible = true
            stage = 6; waitUntil = ticks + 4
        } else if (stage === 6) {
            check(clock.ticking, "showing it again resumes")
            control("/__control/agent", {"session": "rachel", "set": {"latest_state": "idle"}})
            stage = 7
        } else if (stage === 7 && !avatar.authoritativeWorking) {
            stage = 8; waitUntil = ticks + 4
        } else if (stage === 8) {
            check(!clock.ticking, "an idle agent stops the clock")
            last = frame(); stage = 9; waitUntil = ticks + 10
        } else if (stage === 9) {
            check(frame() === last, "and its frame holds still")
            ticker.stop()
            finish()
        }
    }
}
