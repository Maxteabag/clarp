// needs: fake-host
// Teams (list, auto-select, messages, create, leader rule, members, delete)
// and the turn queue (load, edit, send, delete) against the fake Host.
import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
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
            app.loadTeams()
            check(app.teamsLoading, "loading teams")
            stage = 1
        } else if (stage === 1 && !app.teamsLoading && app.teamMessages.length === 1) {
            check(app.teams.length === 1 && app.selectedTeamId === "t1", "the first team is selected automatically")
            check(app.teamMessages[0].text === "Standup at 9", "its messages load")
            check(app.teamNameById("t1") === "Core" && app.teamNameById("zz") === "zz", "team names")
            check(app.teamAgentChoices().length === 2, "agent choices")
            app.updateTeam("t1", "Core", "", "a2")
            check(app.teamsError === "Team leader must already be a member", "leader must be a member")
            app.createTeam("  Growth  ", "#0f0")
            stage = 2
        } else if (stage === 2 && app.teams.length === 2) {
            check(app.teams[1].name === "Growth", "created and reloaded")
            app.addTeamMember("t2", "a2")
            stage = 3
        } else if (stage === 3 && app.teams.length === 2 && app.teams[1].member_agent_ids.length === 1) {
            check(app.teams[1].member_agent_ids[0] === "a2", "member added")
            app.updateTeam("t2", "Growth", "", "a2")
            app.deleteTeam("t1")
            stage = 4
        } else if (stage === 4 && app.teams.length === 1) {
            check(app.teams[0].team_id === "t2" && app.teams[0].leader === "a2", "leader set, old team deleted")
            check(app.selectedTeamId === "t2", "selection moves to a remaining team")
            app.loadTurnQueue("rachel")
            stage = 5
        } else if (stage === 5 && !app.turnQueueLoading && app.turnQueueItems.length === 2) {
            check(app.turnQueueSession === "rachel" && !app.turnQueuePaused, "queue loaded")
            app.updateQueuedTurn("q1", "  sooner  ")
            stage = 6
        } else if (stage === 6 && app.turnQueueItems.length === 2 && app.turnQueueItems[0].text === "sooner") {
            check(true, "edit trims and reloads")
            app.sendQueuedTurn("q1")
            stage = 7
        } else if (stage === 7 && app.turnQueueItems.length === 1) {
            check(app.turnQueueItems[0].queue_id === "q2", "sent item leaves the queue")
            app.deleteQueuedTurn("q2")
            stage = 8
        } else if (stage === 8 && app.turnQueueItems.length === 0 && !app.turnQueueLoading) {
            check(app.turnQueueError === "", "no queue errors")
            ticker.stop()
            finish()
        }
    }
}
