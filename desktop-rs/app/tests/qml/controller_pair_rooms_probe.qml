// needs: fake-host
// env: CLARP_SETTINGS=$SCRATCH/settings.json
// Pair rooms: only pair: conversations are listed, unread until read; a
// room opens without taking Host focus; a member agent's transcript update
// refreshes it; the read revision survives a new controller.
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
    property int roomChanges: 0
    property int reloads: 0
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
    function control(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path, false)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send("{}")
    }
    AppController { id: app; onAgentConversationsChanged: roomChanges++ }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.agentConversations.length === 1) {
            check(app.agentConversations[0].session === "pair:a1:a2" && app.unreadAgentConversations === 1, "one pair room, unread")
            check(app.agentConversation("pair:a1:a2").title === "Rachel & Mike" && app.agentName("pair:a1:a2") === "Rachel & Mike", "room title")
            app.selectSession("pair:a1:a2")
            stage = 1
        } else if (stage === 1 && app.conversation && app.conversation.count === 1) {
            check(app.unreadAgentConversations === 0 && app.selectedName === "Rachel & Mike", "reading it clears unread")
            check(!requests("/select").some(r => r.body.session === "pair:a1:a2"), "no Host focus for a room")
            check(requests("/clips/recoverable").every(r => r.query.session !== "pair:a1:a2"), "no clips for a room")
            app.selectSession("mike")
            control("/__control/pair-message")
            stage = 2
        } else if (stage === 2 && app.unreadAgentConversations === 1 && app.conversationForSession("pair:a1:a2").count === 2) {
            check(true, "a member's update refreshes the room and its transcript")
            second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
            stage = 3
        } else if (stage === 3 && second.agentConversations.length === 1) {
            check(second.unreadAgentConversations === 1, "the unread message is still unread for a new controller")
            app.selectSession("pair:a1:a2")
            stage = 4
        } else if (stage === 4 && app.unreadAgentConversations === 0) {
            second.destroy()
            second = Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
            stage = 5
        } else if (stage === 5 && second.agentConversations.length === 1) {
            check(second.unreadAgentConversations === 0, "the read revision persists")
            roomChanges = 0
            reloads = requests("/agent-conversations").length
            app.loadAgentConversations()
            stage = 51
        } else if (stage === 51 && requests("/agent-conversations").length > reloads && ticks % 8 === 0) {
            check(roomChanges === 0, "an identical room list changes nothing: " + roomChanges)
            const xhr = new XMLHttpRequest()
            xhr.open("POST", app.baseUrl + "/__control/rooms-gone", false)
            xhr.setRequestHeader("Content-Type", "application/json")
            xhr.send(JSON.stringify({"gone": true}))
            app.clearError()
            app.loadAgentConversations()
            stage = 6
        } else if (stage === 6 && app.agentConversations.length === 0) {
            check(app.errorMessage === "" && app.unreadAgentConversations === 0, "an older Host has no rooms and no error banner")
            ticker.stop()
            finish()
        }
    }
}
