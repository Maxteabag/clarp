pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property bool collapsed: false
    property bool showingArchive: false
    property string scope: "all"
    signal openOverview
    signal openConnection
    signal startAgent
    signal hideRequested
    signal chatSelected
    readonly property bool searchOwnsFocus: search.activeFocus
    function clearSearch() { search.clear(); }

    color: "#17151c"

    AgentFilterModel {
        id: roster

        sourceModel: root.showingArchive ? root.controller.archivedAgents : root.controller.agents
        query: root.collapsed || root.showingArchive ? "" : search.text
        unreadOnly: !root.showingArchive && root.scope === "unread"
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 60

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: root.collapsed ? 0 : 16
                anchors.rightMargin: root.collapsed ? 0 : 10
                spacing: 6

                Text {
                    visible: !root.collapsed
                    Layout.fillWidth: true
                    text: "Clarp"
                    color: "#f1ece6"
                    font.pixelSize: 19
                    font.weight: Font.DemiBold
                }

                ToolButton {
                    objectName: "sidebarHideButton"
                    text: "‹"
                    implicitWidth: 24
                    onClicked: root.hideRequested()
                    ToolTip.visible: hovered
                    ToolTip.text: "Hide sidebar · Ctrl+B"
                }

                RoundButton {
                    id: newAgentButton

                    Layout.preferredWidth: 36
                    Layout.preferredHeight: 36
                    Layout.alignment: Qt.AlignHCenter
                    text: root.collapsed ? "›" : "+"
                    onClicked: {
                        if (root.collapsed)
                            root.collapsed = false;
                        else
                            root.startAgent();
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: root.collapsed ? "Expand conversations" : "New agent (Ctrl+N)"

                    background: Rectangle {
                        radius: width / 2
                        color: newAgentButton.pressed ? "#a273c3" : newAgentButton.hovered ? "#c193dd" : "#b884d8"
                    }
                    contentItem: Text {
                        text: newAgentButton.text
                        color: "#17121b"
                        font.pixelSize: 20
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }

        Item {
            visible: !root.collapsed && !root.showingArchive
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 46 : 0

            Rectangle {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                anchors.bottomMargin: 8
                radius: height / 2
                color: "#211e27"
                border.color: search.activeFocus ? "#6f527b" : "#2b2733"
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 8
                    spacing: 8

                    Text {
                        text: "⌕"
                        color: "#8a8391"
                        font.pixelSize: 15
                    }

                    TextField {
                        id: search
                        objectName: "chatListSearch"

                        Layout.fillWidth: true
                        placeholderText: "Search agents"
                        color: "#e9e4df"
                        font.pixelSize: 12
                        background: null
                        leftPadding: 0
                        rightPadding: 0
                        Keys.onEscapePressed: clear()
                        Keys.onDownPressed: {
                            if (chats.count > 0) { chats.currentIndex = 0; chats.forceActiveFocus(); }
                        }
                    }

                    ToolButton {
                        visible: search.text.length > 0
                        text: "×"
                        implicitWidth: 24
                        implicitHeight: 24
                        onClicked: search.clear()
                    }
                }
            }
        }

        RowLayout {
            visible: !root.collapsed && !root.showingArchive
            Layout.fillWidth: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            Layout.bottomMargin: 10
            spacing: 7

            Repeater {
                model: [
                    {
                        "key": "all",
                        "label": "All"
                    },
                    {
                        "key": "unread",
                        "label": "Unread"
                    }
                ]

                AbstractButton {
                    id: chip

                    required property var modelData
                    readonly property bool selected: root.scope === String(chip.modelData.key)

                    implicitHeight: 26
                    implicitWidth: chipLabel.implicitWidth + 24
                    onClicked: root.scope = String(chip.modelData.key)

                    background: Rectangle {
                        radius: height / 2
                        color: chip.selected ? "#b884d8" : "#211e27"
                        border.color: chip.selected ? "transparent" : "#302b37"
                        border.width: 1
                    }
                    contentItem: Text {
                        id: chipLabel

                        text: String(chip.modelData.label)
                        color: chip.selected ? "#17121b" : "#9c95a4"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            Item {
                Layout.fillWidth: true
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: "#221f29"
        }

        ItemDelegate {
            id: archiveRow

            visible: root.controller.archivedAgents.count > 0 && !root.collapsed
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 48 : 0
            leftPadding: 20
            rightPadding: 18
            onClicked: root.showingArchive = !root.showingArchive

            background: Rectangle {
                color: archiveRow.hovered ? "#1d1a23" : "transparent"
            }
            contentItem: RowLayout {
                spacing: 12

                Text {
                    text: root.showingArchive ? "‹" : "▤"
                    color: "#8a8391"
                    font.pixelSize: 14
                }
                Text {
                    Layout.fillWidth: true
                    text: root.showingArchive ? "Back to chats" : "Archived"
                    color: "#c6bfcc"
                    font.pixelSize: 12
                }
                Text {
                    visible: !root.showingArchive
                    text: root.controller.archivedAgents.count
                    color: "#77717f"
                    font.pixelSize: 11
                }
            }
        }

        Rectangle {
            visible: archiveRow.visible
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 1 : 0
            color: "#221f29"
        }

        ListView {
            id: chats

            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: roster
            reuseItems: true
            Keys.onReturnPressed: {
                const item = currentItem as ItemDelegate;
                if (item) item.clicked();
            }
            boundsBehavior: Flickable.StopAtBounds

            delegate: ChatRow {
                controller: root.controller
                collapsed: root.collapsed
                archived: root.showingArchive
                onChatSelected: root.chatSelected()
            }

            ScrollBar.vertical: ScrollBar {}

            Label {
                anchors.centerIn: parent
                width: parent.width - 40
                visible: chats.count === 0
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                text: {
                    if (root.showingArchive)
                        return "Nothing archived.";
                    if (search.text.length > 0)
                        return "No agent matches “" + search.text + "”.";
                    if (root.scope === "unread")
                        return "Nothing unread.";
                    return "No agents yet. Use + to start one.";
                }
                color: "#6f6976"
                font.pixelSize: 12
            }
        }
    }
}
