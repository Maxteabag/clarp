// needs: fake-host
// fixture: notes.txt hello from the probe
// fixture: fail-me.txt this upload is refused
// fixture: shot.png fake image bytes
// fixture: empty.txt
// Composer attachments: upload to the Host, failed uploads, shared-filesystem
// attachments that skip the upload, and sending text plus attachment paths.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    readonly property string hostLog: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-host-log=")) return arg.substring(17)
        return ""
    }
    readonly property string scratch: hostLog.substring(0, hostLog.lastIndexOf("/"))
    function fileUrl(name) { return "file://" + scratch + "/" + name }
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
    function attachments() { return app.composerAttachments("p1", "rachel") }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    property int revisionAtStart: 0
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            app.sharedFilesystem = false
            revisionAtStart = app.composerRevision
            check(app.composerCanSend("p1", "rachel"), "no attachments can send")
            app.attachLocalFile("p1", "rachel", fileUrl("empty.txt"))
            check(attachments().length === 0 && app.errorMessage === "Choose a readable file no larger than 50 MB", "an empty file is refused")
            app.attachLocalFile("p1", "rachel", fileUrl("notes.txt"))
            const pending = attachments()
            check(pending.length === 1 && pending[0].status === "uploading", "upload pending")
            check(app.uploading && !app.composerCanSend("p1", "rachel"), "uploading blocks send")
            check(app.composerRevision > revisionAtStart, "revision bumped")
            check(!app.sendComposerMessage("p1", "rachel", "look", false), "send refused while uploading")
            check(app.errorMessage === "Wait for attachments to finish uploading or remove them", "wait error")
            stage = 1
        } else if (stage === 1 && !app.uploading) {
            const ready = attachments()
            check(ready.length === 1 && ready[0].status === "ready" && ready[0].path === "/srv/uploads/notes.txt", "upload ready")
            const upload = requests("/upload", "POST")
            check(upload.length === 1 && upload[0].body.session === "rachel" && upload[0].body.size === 20
                  && upload[0].body.content_type === "text/plain" && upload[0].body.upload_id === ready[0].id, "upload request")
            check(app.composerAttachments("p1", "mike").length === 0, "attachments are per conversation")
            app.attachLocalFile("p1", "rachel", fileUrl("fail-me.txt"))
            stage = 2
        } else if (stage === 2 && !app.uploading) {
            const list = attachments()
            check(list.length === 2 && list[1].status === "failed", "failed upload is marked")
            check(!app.composerCanSend("p1", "rachel"), "failed attachment blocks send")
            app.removeComposerAttachment("p1", "rachel", list[1].id)
            check(attachments().length === 1 && app.composerCanSend("p1", "rachel"), "removed failed attachment")
            app.sharedFilesystem = true
            app.attachLocalFile("p1", "rachel", fileUrl("shot.png"))
            const local = attachments()
            check(local.length === 2 && local[1].local === true && local[1].status === "ready"
                  && local[1].path === scratch + "/shot.png" && local[1].content_type === "image/png", "shared filesystem attaches in place")
            check(!app.uploading && requests("/upload", "POST").length === 2, "no upload on a shared filesystem")
            app.setPaneDraft("p1", "rachel", "look")
            check(app.sendComposerMessage("p1", "rachel", "  look  ", false), "send accepted")
            check(attachments().length === 0 && app.paneDraft("p1", "rachel") === "", "draft and attachments cleared")
            stage = 3
        } else if (stage === 3 && requests("/send", "POST").length === 1) {
            const sent = requests("/send", "POST")[0].body
            check(sent.text === "look /srv/uploads/notes.txt " + scratch + "/shot.png", "outbound text: " + sent.text)
            ticker.stop()
            finish()
        }
    }
}
