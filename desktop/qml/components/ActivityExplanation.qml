import QtQuick

Item {
    id: root
    property var narrator: null
    property var activity: ({})
    property string session: ""
    readonly property var requestActivity: Object.assign({}, activity, {_session: session})
    property bool active: true
    property Item visualItem: parent
    property bool inViewport: false
    property var requestedNarrator: null
    function release() {
        if (requestedNarrator && requestedNarrator.releaseView) requestedNarrator.releaseView(root);
        requestedNarrator = null;
    }
    function updateVisibility() {
        let viewport = visualItem;
        while (viewport && viewport.objectName !== "transcriptList") viewport = viewport.parent;
        const window = root.Window.window;
        if (!viewport || !visualItem || !visualItem.visible || !active || !narrationEnabled
            || (window && (window.visibility === Window.Hidden || window.visibility === Window.Minimized))) {
            inViewport = false;
            settle.stop();
            release();
            return;
        }
        const point = visualItem.mapToItem(viewport, 0, 0);
        const intersects = viewport.visible && point.y < viewport.height
            && point.y + visualItem.height > 0 && point.x < viewport.width
            && point.x + visualItem.width > 0;
        if (!intersects) { settle.stop(); release(); }
        else if (!inViewport) settle.restart();
        inViewport = intersects;
    }
    property string workingDirectory: ""
    property bool localFilesAllowed: false
    readonly property bool narrationEnabled: narrator !== null && narrator !== undefined && narrator.enabled
    readonly property string text: {
        if (!narrationEnabled) return "";
        narrator.revision;
        return narrator.explanation(requestActivity, workingDirectory, localFilesAllowed);
    }
    property int dotPhase: 0
    readonly property string displayText: !narrationEnabled ? "" : text ||
        (narrator.unavailable ? "Explanation unavailable" : [".", "..", "…"][dotPhase])
    Timer {
        interval: 400; repeat: true
        running: root.narrationEnabled && root.active && root.inViewport
            && root.text.length === 0 && !root.narrator.unavailable
        onTriggered: root.dotPhase = (root.dotPhase + 1) % 3
    }
    function request() {
        if (narrationEnabled && active && inViewport && !settle.running) {
            if (narrator.acquireView) {
                narrator.acquireView(root, requestActivity);
                requestedNarrator = narrator;
            } else narrator.request(requestActivity, workingDirectory, localFilesAllowed);
        }
    }
    function rescheduleActivity() {
        dotPhase = 0;
        release();
        if (inViewport) settle.restart();
    }
    onRequestActivityChanged: rescheduleActivity()
    onNarrationEnabledChanged: updateVisibility()
    onActiveChanged: updateVisibility()
    onWorkingDirectoryChanged: Qt.callLater(request)
    onLocalFilesAllowedChanged: Qt.callLater(request)
    Component.onCompleted: updateVisibility()
    Component.onDestruction: release()
    Timer { id: settle; interval: 180; onTriggered: Qt.callLater(root.request) }
    Timer { interval: 80; repeat: true; running: root.narrationEnabled; onTriggered: root.updateVisibility() }
    property Connections updates: Connections {
        target: root.narrator
        function onChanged() {
            if (root.text.length === 0) Qt.callLater(root.request);
        }
    }
}
