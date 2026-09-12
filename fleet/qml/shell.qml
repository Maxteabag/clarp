import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root
    property bool opened: Quickshell.env("CLARP_FLEET_PANEL_TEST") === "1"
    property bool floatingTest: Quickshell.env("CLARP_FLEET_PANEL_FLOATING_TEST") === "1"
    property bool testMode: Quickshell.env("CLARP_FLEET_PANEL_TEST") === "1"
    property var data: ({peers: [], jobs: [], counts: {}})
    property string error: ""
    property string selectedPeer: ""
    function refresh() { if (!reader.running) reader.running = true }
    function active(status) { return ["reserved", "staging", "running", "cancelling", "unknown"].indexOf(status) >= 0 }
    function reserved(peer, resource) {
        for (let node of root.data.peers || [])
            if (node.id === peer) return (node.reserved || {})[resource] || 0
        return 0
    }
    function runAction(args) {
        if (root.testMode) { root.error = "Test mode: action simulated"; return }
        if (action.running) return
        action.command = ["clarp-fleet"].concat(args)
        action.running = true
    }
    component FleetButton: Button {
        id: control
        padding: 8
        font.pixelSize: 12
        background: Rectangle { color: control.down ? "#37516e" : control.hovered ? "#2c425d" : "#23364e"; radius: 6; border.color: "#49617e" }
        contentItem: Text { text: control.text; color: "#e1ecfa"; font: control.font; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }
    IpcHandler {
        target: "fleet"
        function toggle(): void { root.opened = !root.opened; if (root.opened) root.refresh() }
        function show(): void { root.opened = true; root.refresh() }
        function hide(): void { root.opened = false }
    }
    Process {
        id: reader
        command: root.testMode ? ["cat", Quickshell.env("CLARP_FLEET_PANEL_FIXTURE")] : ["clarp-fleet", "snapshot"]
        stdout: StdioCollector {
            onStreamFinished: {
                try { root.data = JSON.parse(text); root.error = "" }
                catch (e) { root.error = "Fleet data unavailable" }
            }
        }
        stderr: StdioCollector { onStreamFinished: { if (text.length) root.error = "Fleet service unavailable" } }
    }
    Process { id: action; onExited: root.refresh() }
    Timer { interval: 3000; repeat: true; running: root.opened; triggeredOnStart: true; onTriggered: root.refresh() }
    LazyLoader {
        active: root.opened
        component: root.floatingTest ? testWindow : panelWindow
    }
    Component {
        id: testWindow
        FloatingWindow { implicitWidth: 550; implicitHeight: 790; color: "#101827"; Loader { anchors.fill: parent; sourceComponent: panelContent } }
    }
    Component {
        id: panelWindow
        PanelWindow {
            implicitWidth: 550
            implicitHeight: Math.min(790, screen.height - 80)
            anchors { right: true; bottom: true }
            margins { right: 12; bottom: 52 }
            exclusiveZone: 0
            WlrLayershell.layer: WlrLayer.Overlay
            WlrLayershell.keyboardFocus: WlrKeyboardFocus.OnDemand
            color: "#101827"
            Loader { anchors.fill: parent; sourceComponent: panelContent }
        }
    }
    Component {
        id: panelContent
        FocusScope {
            focus: true
            Keys.onEscapePressed: root.opened = false
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 18
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Compute fleet"; color: "#ecf3ff"; font.pixelSize: 25; font.bold: true; Layout.fillWidth: true }
                    FleetButton { text: "Refresh"; onClicked: root.refresh(); Accessible.name: "Refresh fleet" }
                    FleetButton { text: "×"; onClicked: root.opened = false; Accessible.name: "Close fleet panel" }
                }
                Label { text: "Capacity, reservations and returned work"; color: "#9dadc6"; font.pixelSize: 13 }
                Label { visible: root.error.length > 0; text: root.error; color: "#f0af89"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                ScrollView {
                    Layout.fillWidth: true; Layout.fillHeight: true
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: parent.width
                        spacing: 12
                        Repeater {
                            model: root.data.peers || []
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                implicitHeight: hostColumn.implicitHeight + 28
                                radius: 9; color: "#19263b"; border.color: "#334663"
                                ColumnLayout {
                                    id: hostColumn
                                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14 }
                                    spacing: 6
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label { text: modelData.id; color: "#eaf1ff"; font.pixelSize: 18; font.bold: true; Layout.fillWidth: true }
                                        Label { text: modelData.draining ? "Draining" : !modelData.telemetry.reachable ? "Offline" : modelData.age_seconds > 30 ? "Stale" : (modelData.telemetry.cpu_busy_pct || 0) >= 90 ? "Busy" : "Ready"; color: modelData.telemetry.reachable && modelData.age_seconds <= 30 ? "#8de0bb" : "#efb08f" }
                                    }
                                    Label { text: (modelData.telemetry.os || "Unknown OS") + " · " + (modelData.telemetry.arch || "unknown architecture") + " · " + Math.round(modelData.age_seconds) + "s ago"; color: "#9dadc6"; font.pixelSize: 12 }
                                    Label { text: "CPU " + (modelData.telemetry.cpu_busy_pct === null || modelData.telemetry.cpu_busy_pct === undefined ? "unknown" : modelData.telemetry.cpu_busy_pct + "%") + " · " + root.reserved(modelData.id,"cpu") + "/" + modelData.config.capacity.cpu + " slots reserved"; color: "#dce6f7" }
                                    ProgressBar {
                                        id: cpuProgress
                                        Layout.fillWidth: true; implicitHeight: 6; from: 0; to: Math.max(1,modelData.config.capacity.cpu); value: root.reserved(modelData.id,"cpu")
                                        background: Rectangle { color: "#2b3d56"; radius: 3 }
                                        contentItem: Item { Rectangle { width: parent.width * cpuProgress.visualPosition; height: parent.height; radius: 3; color: "#72d7c9" } }
                                    }
                                    Label { text: "RAM " + (modelData.telemetry.ram_available_mb === null || modelData.telemetry.ram_available_mb === undefined ? "unknown" : (modelData.telemetry.ram_available_mb/1024).toFixed(1) + " GiB available") + " · " + (root.reserved(modelData.id,"ram_mb")/1024).toFixed(1) + " GiB reserved"; color: "#dce6f7"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                                    Label { text: modelData.telemetry.gpu_name ? modelData.telemetry.gpu_name + " · " + (modelData.telemetry.gpu_free_mb === null ? "memory unknown/shared" : Math.round(modelData.telemetry.gpu_free_mb) + " MiB free") : "GPU telemetry unavailable"; color: "#9dadc6"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                                    Label { text: (modelData.telemetry.profiles || []).join(" · "); color: "#7fd6d0"; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 12 }
                                    Label { text: "Interactive reserve: " + modelData.config.interactive_cpu + " CPU · " + (modelData.config.interactive_ram_mb/1024).toFixed(1) + " GiB"; color: "#9dadc6"; font.pixelSize: 12 }
                                    FleetButton { text: modelData.config.interactive_cpu > 2 ? "Restore normal background budget" : "Reserve capacity for me"; onClicked: root.runAction(["limits", modelData.id, "--interactive-cpu", String(modelData.config.interactive_cpu > 2 ? 2 : Math.max(0,modelData.config.capacity.cpu-1)), "--interactive-ram-mb", String(modelData.config.interactive_cpu > 2 ? 2048 : Math.min(4096,modelData.config.capacity.ram_mb))]) }
                                    FleetButton { text: modelData.draining ? "Resume accepting jobs" : "Drain — finish existing jobs"; onClicked: root.runAction(modelData.draining ? ["drain",modelData.id,"--resume"] : ["drain",modelData.id]) }
                                }
                            }
                        }
                        Label { text: "Other tailnet peers"; color: "#ecf3ff"; font.pixelSize: 17; font.bold: true }
                        Repeater {
                            model: (root.data.discovered || []).filter(peer => !peer.enrolled)
                            delegate: Label { required property var modelData; text: modelData.name + " · " + modelData.os + " · " + (modelData.online ? "online, not enrolled" : "offline"); color: "#9dadc6"; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 12 }
                        }
                        Label { text: "Recent jobs"; color: "#ecf3ff"; font.pixelSize: 19; font.bold: true; Layout.topMargin: 8 }
                        Label { visible: !(root.data.jobs || []).length; text: "No jobs submitted"; color: "#9dadc6" }
                        Repeater {
                            model: root.data.jobs || []
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                implicitHeight: jobColumn.implicitHeight + 20
                                radius: 7; color: "#152135"
                                ColumnLayout {
                                    id: jobColumn
                                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10 }
                                    Label { text: modelData.request.profile + " → " + (modelData.peer || "queued"); color: "#eaf1ff"; font.bold: true }
                                    Label { text: modelData.status + " · " + modelData.request.parent.host + "/" + modelData.request.parent.agent; color: "#a5b8d1"; elide: Text.ElideRight; Layout.fillWidth: true }
                                    Label { visible: !!modelData.error; text: modelData.error || ""; color: "#efb08f"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                                    Label { text: modelData.id; color: "#7188a7"; elide: Text.ElideMiddle; Layout.fillWidth: true; font.pixelSize: 11 }
                                    FleetButton { visible: root.active(modelData.status) || modelData.status === "queued"; text: "Cancel this job"; onClicked: root.runAction(["cancel",modelData.id]) }
                                }
                            }
                        }
                    }
                }
                Label { text: "Escape closes · Budgets apply to new jobs"; color: "#8297b6"; font.pixelSize: 12 }
            }
        }
    }
}
