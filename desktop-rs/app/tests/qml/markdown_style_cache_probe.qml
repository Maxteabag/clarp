// tst_native_core::hugeMarkdownBlocksAreNotRetainedInTheStyleCache: a huge
// block is rendered but not cached, and the cache stays under its 4 MB
// bound, dropping entries rather than growing.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    AppController { id: app }
    Timer {
        interval: 20; running: true
        onTriggered: {
            const style = {bodyPixelSize: 15, bodyFamily: "JetBrains Mono", monoFamily: "JetBrains Mono"}
            const cached = () => app.memoryCounters().styledMarkdownCacheBytes
            check(app.styledMarkdownHtml("hello **world**", style).indexOf("world") >= 0, "renders")
            const first = cached()
            check(first > 0, "a small block is cached: " + first)
            check(app.styledMarkdownHtml("# Huge\n\n" + "x".repeat(17 * 1024), style).indexOf("Huge") >= 0, "a huge block renders")
            check(cached() === first, "but is not retained: " + cached())
            let previous = first, bounded = false, largest = 0
            // The Rust cache counts the UTF-8 it holds (the C++ one UTF-16), so
            // it takes more rows than the C++ test to reach the bound.
            for (let i = 0; i < 1500; ++i) {
                const markdown = "## Row " + i + "\n\n" + String.fromCharCode(97 + i % 26).repeat(1800)
                if (app.styledMarkdownHtml(markdown, style).indexOf("Row") < 0) { check(false, "row " + i + " renders"); break }
                const now = cached()
                largest = Math.max(largest, now)
                if (now < previous) bounded = true
                previous = now
            }
            check(largest <= 4 * 1024 * 1024, "the cache never passes 4 MB: " + largest)
            check(bounded, "the bound drops entries")
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
