// needs: fake-host
// env: CLARP_SETTINGS=$SCRATCH/settings.json
// Ports of tst_native_core paneDraftIsDurableAndScopedToServerAndConversation
// (text part), paneDraftAndFocusSurviveLayoutStateChanges,
// paneActivationAlwaysTargetsItsComposer, the showWhenReady persistence in
// readyPresentationRetainsCanonicalStreamAndRevealsFinal,
// unreachableHostErrorClearsWhenItIsBack and
// connectedControllerShutsDownWithoutLateSseCallbacks.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    property var doomed: null
    property var stamp: 0
    property string firstPane: ""
    property string secondPane: ""

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function controller() {
        return Qt.createQmlObject('import Clarp.Desktop; AppController {}', root)
    }
    function post(path, body) {
        const xhr = new XMLHttpRequest()
        xhr.open("POST", app.baseUrl + path)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(body))
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }

    id: root
    AppController { id: app }

    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 1200) { check(false, "timed out at stage " + stage); stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); stop(); finish() }
        }
    }

    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            app.setPaneDraft("pane-a", "rachel", "durable thought")
            check(app.paneDraft("pane-b", "rachel") === "durable thought", "a draft follows the chat to any pane")
            check(app.paneDraft("pane-a", "bella") === "", "drafts are scoped to the conversation")
            const early = controller()
            check(early.paneDraft("x", "rachel") === "", "typing does not persist per keystroke")
            early.destroy()
            app.flushPendingDrafts()
            const relaunched = controller()
            check(relaunched.paneDraft("new-pane", "rachel") === "durable thought", "flushed draft survives a relaunch")
            relaunched.setPaneDraft("new-pane", "rachel", "")
            relaunched.flushPendingDrafts()
            check(relaunched.paneDraft("new-pane", "rachel") === "", "clearing removes the draft")
            relaunched.destroy()

            app.showWhenReady = true
            const restored = controller()
            check(restored.showWhenReady, "showWhenReady is restored by the next controller")
            restored.destroy()
            app.showWhenReady = false

            firstPane = app.panes.activePaneId
            check(app.composerFocusPane === firstPane, "composer focus starts on the active pane")
            app.setPaneDraft(firstPane, "rachel", "Unsent thought")
            app.panes.splitActive("vertical", "mike")
            secondPane = app.panes.activePaneId
            stage = 1
        } else if (stage === 1 && app.composerFocusPane === secondPane) {
            check(secondPane !== firstPane, "split opened a new pane with focus")
            app.requestComposerFocus("")
            app.panes.focusPane(firstPane)
            stage = 2
        } else if (stage === 2 && app.composerFocusPane === firstPane) {
            check(true, "activating a pane targets its composer")
            app.panes.toggleZoom()
            check(app.paneDraft(firstPane, "rachel") === "Unsent thought" && app.panes.zoomedPaneId === firstPane,
                  "draft and zoom survive layout changes")
            app.panes.toggleZoom()
            app.setPaneDraft(firstPane, "rachel", "")
            doomed = controller()
            stage = 3
        } else if (stage === 3 && doomed.connected && doomed.agents.count === 2) {
            doomed.destroy()
            doomed = null
            stamp = ticks
            stage = 4
        } else if (stage === 4 && ticks - stamp > 20) {
            check(true, "a connected controller shut down without late callbacks")
            post("/__control/outage", {seconds: 3})
            stage = 5
        } else if (stage === 5 && !app.connected) {
            app.refreshAgents()
            stage = 6
        } else if (stage === 6 && app.errorMessage !== "") {
            check(true, "an outage surfaces an error: " + app.errorMessage)
            stage = 7
        } else if (stage === 7 && app.connected && app.errorMessage === "") {
            check(true, "the transport error clears once the Host is back")
            ticker.stop()
            finish()
        }
    }
}
