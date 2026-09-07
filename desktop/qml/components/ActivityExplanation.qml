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
    // A translation that will not arrive — this row failed, or the narrator as a
    // whole is in error. Either way the card has something better to show than a
    // dead end: the tool call it was translating.
    readonly property bool failed: {
        if (!narrationEnabled) return false;
        narrator.revision;
        return narrator.unavailable || narrator.failed(requestActivity, workingDirectory, localFilesAllowed);
    }
    // What the card should actually render as narration. Consumers gate the
    // original tool name, summary and detail on this rather than on
    // narrationEnabled, so a failure falls back instead of blanking the row.
    readonly property bool narrationShown: narrationEnabled && !failed
    property int dotPhase: 0
    readonly property string displayText: !narrationShown ? "" : text || [".", "..", "…"][dotPhase]
    Timer {
        interval: 400; repeat: true
        running: root.narrationShown && root.active && root.inViewport && root.text.length === 0
        onTriggered: root.dotPhase = (root.dotPhase + 1) % 3
    }
    function request() {
        // A failed row keeps an empty text, so without this guard the retry
        // below would re-request it on every narrator update, forever.
        if (narrationShown && active && inViewport && !settle.running) {
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
            if (root.text.length === 0 && !root.failed) Qt.callLater(root.request);
        }
    }
}
