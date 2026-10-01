// needs: fake-host
// needs: keyring
// env: CLARP_TOKEN=
// Pairing, the keyring-held device token and forgetting it. The token is
// empty, so startup asks the (throwaway) keyring instead of any config file.
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
        if (stage === 0 && app.connectionState === "unauthorized") {
            check(!app.hasStoredCredential, "an empty keyring holds no credential")
            app.pairDevice(app.baseUrl, "  ")
            check(app.errorMessage === "Enter the one-time pairing code", "a code is required")
            app.pairDevice(app.baseUrl, " 999999 ")
            check(app.connectionState === "pairing" && app.connecting, "pairing")
            stage = 1
        } else if (stage === 1 && !app.connecting) {
            check(app.connectionState === "offline" && app.errorMessage.indexOf("pairing code expired") >= 0, "rejected code: " + app.errorMessage)
            const exchange = requests("/pairing/exchange", "POST")[0]
            check(exchange.body.code === "999999" && exchange.body.device_name === "Clarp desktop" && exchange.authorization === "", "exchange request without a bearer")
            app.pairDevice(app.baseUrl, "000000")
            stage = 2
        } else if (stage === 2 && !app.connecting) {
            check(app.errorMessage === "Pairing response did not contain a device credential" && app.connectionState === "offline", "a reply without a token")
            app.pairDevice(app.baseUrl, "123456")
            stage = 3
        } else if (stage === 3 && app.connected && app.hasStoredCredential) {
            check(app.errorMessage === "", "paired and connected")
            check(requests("/server-info", "GET").pop().authorization === "Bearer cld_probe_paired_device", "requests carry the device token")
            // A fresh controller finds the stored token on its own.
            second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
            stage = 4
        } else if (stage === 4 && second.connected) {
            check(second.hasStoredCredential && second.errorMessage === "", "a new controller connects from the keyring")
            second.destroy()
            app.forgetCredential()
            stage = 5
        } else if (stage === 5 && !app.hasStoredCredential) {
            check(app.connectionState === "offline" && !app.connecting && app.errorMessage === "", "forgotten and offline: " + app.connectionState + " " + app.connecting + " [" + app.errorMessage + "]")
            app.connectToServer(app.baseUrl, "")
            stage = 6
        } else if (stage === 6 && app.connectionState === "unauthorized") {
            check(!app.hasStoredCredential, "the keyring no longer has the token")
            ticker.stop()
            finish()
        }
    }
}
