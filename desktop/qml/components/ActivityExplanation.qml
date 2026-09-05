import QtQuick

QtObject {
    id: root
    property var narrator: null
    property var activity: ({})
    property bool active: true
    readonly property bool enabled: narrator !== null && narrator !== undefined && narrator.enabled
    readonly property string text: {
        if (!enabled) return "";
        narrator.revision;
        return narrator.explanation(activity);
    }
    function request() {
        if (enabled && active) narrator.request(activity);
    }
    onActivityChanged: Qt.callLater(request)
    onEnabledChanged: Qt.callLater(request)
    onActiveChanged: Qt.callLater(request)
    Component.onCompleted: Qt.callLater(request)
    property Connections updates: Connections {
        target: root.narrator
        function onChanged() {
            if (root.text.length === 0) Qt.callLater(root.request);
        }
    }
}
