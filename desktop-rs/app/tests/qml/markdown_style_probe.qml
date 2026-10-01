// styledMarkdownHtml from QML: the options MessageDelegate passes, rich text
// a TextEdit lays out, cached on the second call.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 400; height: 400; visible: true
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    AppController { id: app }
    TextEdit { id: view; width: 380; textFormat: Text.RichText; wrapMode: Text.Wrap }
    Timer {
        interval: 20; running: true
        onTriggered: {
            const options = {bodyPixelSize: 16, bodyFamily: "Atkinson Hyperlegible", monoFamily: "JetBrains Mono",
                             codeBackground: "#ff123456", quoteText: "#abcdef", link: "#82aaff", rule: "#303342"}
            const markdown = "# Title\n\nText with `code` and a [link](https://example.com).\n\n```\nblock line\n```\n\n> quoted\n\n- one\n- two\n"
            const html = app.styledMarkdownHtml(markdown, options)
            check(html.indexOf("font-size:21px") >= 0 && html.indexOf("#ff123456") >= 0 && html.indexOf("Atkinson Hyperlegible") >= 0, "options reach the renderer")
            check(app.styledMarkdownHtml(markdown, options) === html, "the same row renders the same")
            view.text = html
            const plain = view.getText(0, view.length)
            check(plain.indexOf("Title") === 0 && plain.indexOf("block line") > 0 && plain.indexOf("quoted") > 0, "rich text lays out: " + JSON.stringify(plain.slice(0, 80)))
            check(view.lineCount >= 6 && view.contentHeight > 100, "several lines: " + view.lineCount)
            check(app.markdownDisplayBlocks("one\n\ntwo").length === 2, "display blocks")
            console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
            Qt.exit(failures === 0 ? 0 : 1)
        }
    }
}
