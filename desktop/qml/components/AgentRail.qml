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
    color: "#17151c"
    readonly property bool searchOwnsFocus: chatList.searchOwnsFocus
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
