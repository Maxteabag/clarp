// fixture: report.html <html><body>Report</body></html>
// fixture: launcher.desktop [Desktop Entry]\nExec=bad
// fixture: hidden.txt [Desktop Entry] Exec=bad
// fixture: program.txt #!/bin/sh echo bad
// fixture: unknown.xyz plain text
// fixture: report.pdf %PDF-1.4
// fixture: report.txt plain text
// fixture: report.svg <svg xmlns="http://www.w3.org/2000/svg"></svg>
// fixture-exec: executable.html <html>Report</html>
// fixture-dir: folder
// tst_native_core::localReportsRequireOriginAndSafeReadableFiles: a local
// report opens only with a shared filesystem, from this Host, and only for
// a readable, non-executable file whose content matches its name.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    readonly property string dir: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17).replace(/host\.log$/, "")
        return ""
    }
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function opened() {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + dir + "opened-urls", false)
        xhr.send()
        return xhr.responseText.split("\n").filter(l => l)
    }
    AppController { id: app }
    Timer {
        interval: 20; running: true
        onTriggered: {
            const origin = app.baseUrl, html = dir + "report.html"
            app.sharedFilesystem = false
            check(!app.openLocalReport(html, origin), "no shared filesystem, no local report")
            app.sharedFilesystem = true
            check(!app.openLocalReport(html, "http://different.invalid"), "another Host's report is refused")
            check(!app.openLocalReport(html, ""), "a report with no origin is refused")
            for (const name of ["missing.txt", "folder", "launcher.desktop", "hidden.txt", "program.txt", "unknown.xyz", "executable.html"])
                check(!app.openLocalReport(dir + name, origin), name + " is refused")
            check(!app.openLocalReport("file://remote/report.html", origin), "a remote file URL is refused")
            check(!app.openLocalReport("javascript:alert(1)", origin), "a script URL is refused")
            check(opened().length === 0, "nothing opened so far")
            check(app.openExternalLink(html, origin), "a report path in a transcript link opens")
            check(opened().slice(-1)[0] === "file://" + html, "as its canonical file: " + opened().slice(-1)[0])
            check(app.openLocalReport("file://" + html, origin), "a file URL to the report opens")
            for (const name of ["report.pdf", "report.txt", "report.svg"])
                check(app.openLocalReport(dir + name, origin), name + " opens")
            app.sharedFilesystem = false
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
