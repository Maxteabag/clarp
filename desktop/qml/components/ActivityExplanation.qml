import QtQuick

QtObject {
    id: root
    property var narrator: null
    property var activity: ({})
    property string session: ""
    readonly property var requestActivity: Object.assign({}, activity, {_session: session})
    property bool active: true
    property string workingDirectory: ""
    property bool localFilesAllowed: false
    readonly property bool enabled: narrator !== null && narrator !== undefined && narrator.enabled
    readonly property string text: {
        if (!enabled) return "";
        narrator.revision;
        return narrator.explanation(requestActivity, workingDirectory, localFilesAllowed);
    }
    readonly property string displayText: !enabled ? "" : text ||
        (narrator.unavailable ? "Explanation unavailable" : "Explaining activity…")
    function request() {
        if (enabled && active) narrator.request(requestActivity, workingDirectory, localFilesAllowed);
    }
    onActivityChanged: Qt.callLater(request)
    onSessionChanged: Qt.callLater(request)
    onEnabledChanged: Qt.callLater(request)
    onActiveChanged: Qt.callLater(request)
    onWorkingDirectoryChanged: Qt.callLater(request)
    onLocalFilesAllowedChanged: Qt.callLater(request)
    Component.onCompleted: Qt.callLater(request)
    property Connections updates: Connections {
        target: root.narrator
        function onChanged() {
            if (root.text.length === 0) Qt.callLater(root.request);
        }
    }
}
