// needs: fake-host
// Past sessions (with generation fencing and failures), resuming one, launch
// directories, directory suggestions, favorite paths and contact assignment.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var requested: []
    property var assigned: []
    property int launchQueryTick: 0
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
    AppController {
        id: app
        onContactAssignmentRequested: (session, automatic) => requested.push(session + ":" + automatic)
        onContactAssignmentSucceeded: session => assigned.push(session)
    }
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
            app.loadPastSessions("  ", "codex", false)
            check(!app.pastSessionsLoading, "a blank directory loads nothing")
            app.loadPastSessions("/broken", "codex", false)
            app.loadPastSessions("/slow", "codex", true)
            check(app.pastSessionsLoading && app.pastSessions.length === 0, "past sessions loading")
            stage = 1
        } else if (stage === 1 && !app.pastSessionsLoading) {
            check(app.pastSessions.length === 2 && app.pastSessions[1].session_id === "old-2", "all-project sessions")
            check(app.errorMessage === "", "the superseded failure stays quiet: " + app.errorMessage)
            // Both requests are in flight together, so find this one by directory.
            const query = requests("/past-sessions", "GET").map(r => r.query).find(q => q.cwd === "/slow")
            check(query && query.backend === "codex" && query.scope === "all", "past-session query")
            app.loadPastSessions("/broken", "codex", false)
            stage = 2
        } else if (stage === 2 && !app.pastSessionsLoading) {
            check(app.pastSessions.length === 0 && app.errorMessage.indexOf("history unreadable") >= 0, "current failure surfaces: " + app.errorMessage)
            app.setLaunchDirectory("/work")
            check(app.resumeLaunchSession("codex", "old-1", false), "resume accepted")
            check(app.startingContact === "resume", "starting resume")
            check(!app.resumeLaunchSession("codex", "old-1", false), "second resume refused while starting")
            stage = 3
        } else if (stage === 3 && app.startingContact === "") {
            const body = requests("/agents", "POST").pop().body
            check(body.resume_session_id === "old-1" && body.open_existing === true && body.auto_contact === true
                  && !("anonymous" in body) && body.cwd === "/work" && body.synthesize_audio === false, "resume request shape")
            launchQueryTick = ticks
            app.loadLaunchDirectories("slow")
            app.loadLaunchDirectories("proj")
            check(app.launchDirectoriesLoading, "launch directories loading")
            app.loadDirectorySuggestions("/home/fake")
            app.loadFavoritePaths()
            stage = 4
        } else if (stage === 4 && !app.launchDirectoriesLoading && app.directorySuggestions.length === 2 && app.favoritePaths.length === 2) {
            check(app.launchDirectories.length === 1 && app.launchDirectories[0].label === "proj", "latest launch directory query wins")
            check(app.lastWorkingDirectory === "/work", "the resumed directory is kept over the Host home: " + app.lastWorkingDirectory)
            check(app.directorySuggestions[0] === "/home/fake/one", "directory suggestions")
            check(requests("/favorite-paths", "GET")[0].query.limit === "5", "favorites limited to 5")
            app.loadDirectorySuggestions(" ")
            check(app.directorySuggestions.length === 0, "blank path clears suggestions")
            app.requestContactAssignment("rachel", true)
            check(requested.length === 1 && requested[0] === "rachel:true", "assignment request signal")
            app.loadAssignmentContacts("rachel")
            stage = 5
        } else if (stage === 5 && app.assignmentContacts.length === 1) {
            check(app.assignmentContacts[0].name === "Paula", "assignment contacts")
            app.assignContact("", "choose", "Paula")
            check(app.errorMessage === "Connect and select an agent before assigning a contact", "assignment needs a session")
            app.assignContact("rachel", "choose", "Paula")
            stage = 6
        } else if (stage === 6 && assigned.length === 1) {
            check(assigned[0] === "rachel" && app.errorMessage === "", "assignment succeeded")
            const body = requests("/agent-assign", "POST").pop().body
            check(body.mode === "choose" && body.name === "Paula", "assignment request shape")
            app.assignContact("rachel", "choose", "Nobody")
            stage = 7
        } else if (stage === 7 && app.errorMessage !== "") {
            check(app.errorMessage.indexOf("contact is busy") >= 0 && assigned.length === 1, "assignment failure: " + app.errorMessage)
            stage = 8
        } else if (stage === 8 && ticks > launchQueryTick + 40) {
            // Well past the Host's 0.4 s delay on the superseded "slow" query.
            check(app.launchDirectories.length === 1 && app.launchDirectories[0].label === "proj", "the superseded query's late reply is dropped")
            ticker.stop()
            finish()
        }
    }
}
