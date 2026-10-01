// tst_palette::overridesLightHostInEveryState: the real Main over the
// offscreen platform's light palette keeps dark control surfaces and light
// text in the active, inactive and disabled states.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function lightness(c) { return (Math.max(c.r, c.g, c.b) + Math.min(c.r, c.g, c.b)) / 2 }
    Timer {
        interval: 300; running: true
        onTriggered: {
            const groups = {active: root.palette.active, inactive: root.palette.inactive, disabled: root.palette.disabled}
            for (const name in groups) {
                const g = groups[name]
                for (const role of ["window", "base", "alternateBase", "button", "toolTipBase"])
                    check(lightness(g[role]) < 0.25, name + " " + role + " is dark: " + g[role])
                check(lightness(g.text) > 0.45 && lightness(g.buttonText) > 0.45, name + " text is light: " + g.text + " " + g.buttonText)
            }
            check(Qt.colorEqual(root.palette.active.window, "#1a1b26"), "the default theme's window: " + root.palette.active.window)
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
