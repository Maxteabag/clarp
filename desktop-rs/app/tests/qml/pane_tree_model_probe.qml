// Offscreen check of the Rust PaneTreeModel: QML-visible layout properties
// and layout writes that land from the worker thread. Conflict and recovery
// rules are covered in core/tests/panes.rs; QML's file PUT is not reliably
// synchronous, so it cannot stand in for a second window here.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int treeSignals: 0
    property int stage: 0
    property int polls: 0
    readonly property string storePath: {
        for (const arg of Qt.application.arguments)
            if (arg.startsWith("--probe-store=")) return arg.substring(14)
        return ""
    }

    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function readFile(path) {
        const xhr = new XMLHttpRequest()
        xhr.open("GET", "file://" + path, false)
        xhr.send()
        return xhr.status === 200 || xhr.status === 0 ? xhr.responseText : ""
    }
    function savedSession() {
        const text = readFile(storePath)
        if (!text) return ""
        const collection = JSON.parse(JSON.parse(text).collectionV1)
        const state = collection.states[collection.active]
        const find = node => node.id === state.activePaneId ? node
            : node.kind === "split" ? (find(node.first) || find(node.second)) : null
        const active = find(state.root)
        return active ? active.session : ""
    }
    // True once the store holds exactly the model's current layout, i.e. no
    // write is still in flight. The external write below takes no lock, so it
    // must not race a worker that already read the file.
    function settled() {
        const text = readFile(storePath)
        if (!text) return false
        const collection = JSON.parse(JSON.parse(text).collectionV1)
        return JSON.stringify(collection.states[collection.active].root) === JSON.stringify(panes.rootNode)
            && collection.states[collection.active].activePaneId === panes.activePaneId
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }

    PaneTreeModel {
        id: panes
        onTreeChanged: treeSignals++
    }

    Component.onCompleted: {
        try {
            check(storePath !== "", "runner passed a scratch store")
            panes.setActiveSession("rachel")
            check(panes.activeSession === "rachel" && panes.paneCount === 1, "one pane shows rachel")
            panes.splitActive("vertical", "bella")
            check(panes.paneCount === 2 && panes.activeSession === "bella", "split adds bella")
            check(panes.rootNode.kind === "split" && panes.rootNode.direction === "vertical", "rootNode is a JS object")
            check(panes.paneLayout.length === 2 && Math.abs(panes.paneLayout[1].x - 0.5) < 1e-9, "paneLayout rectangles")
            check(panes.splitLayout.length === 1, "splitLayout")
            panes.navigate("left")
            check(panes.activeSession === "rachel", "navigate left")
            panes.toggleZoom()
            check(panes.displayRoot.kind === "leaf" && panes.zoomedPaneId === panes.activePaneId, "zoom")
            check(panes.viewLayout.filter(p => p.shown).length === 1, "zoom hides the other pane")
            panes.toggleZoom()
            check(treeSignals > 0, "treeChanged fires")
            check(panes.workspaces.length === 1 && panes.workspaces[0].name === "Main", "workspaces list")
            check(panes.saveState().activePaneId === panes.activePaneId, "saveState")
        } catch (e) { failures++; console.log("FAIL exception: " + e) }
        stage = 1
        poll.start()
    }

    // Writes run on a worker thread; poll the store instead of assuming.
    Timer {
        id: poll
        interval: 20; repeat: true
        onTriggered: {
            if (++polls > 150) { check(false, "timed out at stage " + stage); stop(); finish(); return }
            try {
                if (stage === 1 && savedSession() === "rachel" && settled()) {
                    check(panes.workspaceSaveWarning === "", "saved from the worker without warning")
                    panes.splitActive("horizontal", "omar")
                    panes.createWorkspace("Review")
                    stage = 2
                } else if (stage === 2 && settled()) {
                    const collection = JSON.parse(JSON.parse(readFile(storePath)).collectionV1)
                    check(Object.keys(collection.names).length === 2, "both workspaces saved")
                    check(panes.workspaces.length === 2, "workspaces property follows")
                    panes.setActiveSession("beta")
                    panes.saveWorkspaceLayoutInstead()
                    stage = 3
                } else if (stage === 3 && savedSession() === "beta" && settled() && panes.workspaceSaveWarning === "") {
                    check(true, "forced save lands and leaves no warning")
                    stop()
                    finish()
                }
            } catch (e) { failures++; console.log("FAIL exception: " + e); stop(); finish() }
        }
    }
}
