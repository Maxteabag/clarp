import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    required property var controller
    property string selectedSurface: "chats"
    signal openOverview
    signal hideRequested
    signal openSwitcher
    signal startAgent
    signal selectSurface(string surface)
    color: "#20212e"
    readonly property bool searchOwnsFocus: chatList.searchOwnsFocus
    readonly property string keyboardSession: chatList.keyboardSession
    readonly property int rowCount: chatList.rowCount
    function ownsFocus(item) {
        for (let current = item; current; current = current.parent) {
            if (current === root) return true;
        }
        return false;
    }
    function focusCurrentAgent() { chatList.focusCurrentAgent(); }
    function moveSelection(delta) { chatList.moveSelection(delta); }
    function openSelection() { chatList.openSelection(); }
    function focusSearch() { chatList.focusSearch(); }
    function clearSearch() { chatList.clearSearch(); }
    RowLayout {
        anchors.fill: parent
        spacing: 0
        NavRail {
            Layout.fillHeight: true
            controller: root.controller
            selectedSurface: root.selectedSurface
            onSelectSurface: surface => root.selectSurface(surface)
            onOpenOverview: root.openOverview()
            onOpenConnection: root.selectSurface("settings")
            onOpenSwitcher: root.openSwitcher()
        }
        ChatList {
            id: chatList
            Layout.fillWidth: true
            Layout.fillHeight: true
            controller: root.controller
            onOpenOverview: root.openOverview()
            onOpenConnection: root.selectSurface("settings")
            onHideRequested: root.hideRequested()
            onStartAgent: root.startAgent()
            onChatSelected: root.selectSurface("chats")
        }
    }
}
