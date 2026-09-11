pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    property int selectedIndex: 0
    property bool confirmWhenReady: false
    readonly property var choices: {
        if (!controller.connected || queryTimer.running || controller.launchDirectoriesLoading)
            return search.text.trim() ? [] : [{path:"~",label:"~"}];
        const found = controller.launchDirectories;
        return found.length ? found : search.text.trim() ? [] : [{path:"~",label:"~"}];
    }
    signal chosen(string path, string label)
    signal cancelRequested
    spacing: 12
    function open() {
        search.clear(); selectedIndex = 0; confirmWhenReady = false;
        reload(); Qt.callLater(() => search.forceActiveFocus());
    }
    function focusSearch() { search.forceActiveFocus(); }
    function reload() {
        if (controller.connected) controller.loadLaunchDirectories(search.text.trim());
    }
    function back() {
        if (search.text.length) { search.clear(); selectedIndex = 0; confirmWhenReady = false; reload(); }
        else cancelRequested();
    }
    function confirm() {
        if (!search.text.trim() && selectedIndex === 0) { chosen("~", "~"); return; }
        if (queryTimer.running) { confirmWhenReady = true; queryTimer.stop(); reload(); return; }
        if (controller.launchDirectoriesLoading || !controller.connected) { confirmWhenReady = true; return; }
        if (!choices.length) return;
        const choice = choices[Math.min(selectedIndex, choices.length - 1)];
        confirmWhenReady = false; chosen(choice.path, choice.label);
    }
    function move(delta) {
        selectedIndex = Math.max(0, Math.min(selectedIndex + delta,
            queryTimer.running || controller.launchDirectoriesLoading ? 15 : Math.max(0, choices.length - 1)));
        results.positionViewAtIndex(selectedIndex, ListView.Contain);
    }
    TuiTextField {
        id: search
        objectName: "launchDirectorySearch"
        Layout.fillWidth: true
        placeholderText: "~"
        selectByMouse: true
        onTextEdited: { root.selectedIndex = 0; root.confirmWhenReady = false; queryTimer.restart(); }
        onAccepted: root.confirm()
        Keys.onPressed: event => {
            switch (event.key) {
            case Qt.Key_Down: root.move(1); break;
            case Qt.Key_Up: root.move(-1); break;
            case Qt.Key_Tab:
                if (root.choices.length && !queryTimer.running && !root.controller.launchDirectoriesLoading) {
                    search.text = root.choices[Math.min(root.selectedIndex, root.choices.length - 1)].path + "/";
                    root.selectedIndex = 0; root.reload();
                }
                break;
            default: return;
            }
            event.accepted = true;
        }
    }
    ListView {
        id: results
        objectName: "launchDirectoryResults"
        Layout.fillWidth: true
        Layout.preferredHeight: 224
        clip: true
        model: root.choices
        currentIndex: Math.min(root.selectedIndex, count - 1)
        delegate: TuiText {
            required property var modelData
            required property int index
            width: results.width; height: 32
            text: modelData.label
            leftPadding: 10
            verticalAlignment: Text.AlignVCenter
            color: "#c0caf5"; elide: Text.ElideMiddle
            Rectangle { anchors.fill: parent; z: -1; color: parent.index === results.currentIndex ? "#303348" : "transparent" }
            MouseArea { anchors.fill: parent; onClicked: root.chosen(parent.modelData.path, parent.modelData.label) }
        }
        TuiText {
            anchors.centerIn: parent
            visible: root.controller.connected && !queryTimer.running && !root.controller.launchDirectoriesLoading && results.count === 0
            text: "No matching directories"; color: "#9ca1bd"
        }
    }
    Timer { id: queryTimer; interval: 80; onTriggered: root.reload() }
    Connections {
        target: root.controller
        function onConnectedChanged() { if (root.visible && root.controller.connected) root.reload(); }
        function onLaunchDirectoriesChanged() {
            if (root.visible && !root.controller.launchDirectoriesLoading && !queryTimer.running && root.confirmWhenReady)
                root.confirm();
        }
        function onErrorMessageChanged() {
            if (root.controller.errorMessage.length) root.confirmWhenReady = false;
        }
    }
}
