pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    signal openChat(string session)
    color: "#171923"
    objectName: "teamsPanel"

    function selectedTeam() {
        for (const team of controller.teams) {
            if (String(team.team_id || "") === controller.selectedTeamId)
                return team;
        }
        return null;
    }

    function teamLeaderChoices() {
        const team = root.selectedTeam();
        const members = team ? Array.from(team.member_agent_ids || []) : [];
        return [{ id: "", name: "No leader" }].concat(
            root.controller.teamAgentChoices().filter(choice =>
                members.includes(String(choice.id || ""))));
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 54
            Layout.leftMargin: 16
            Layout.rightMargin: 12
            spacing: 9

            Text {
                text: "TEAMS"
                color: "#c9cde3"
                font.family: "JetBrains Mono"
                font.pixelSize: 19
                font.weight: Font.DemiBold
                font.letterSpacing: 1.5
            }
            Text {
                text: root.controller.teams.length + " teams"
                color: "#656a82"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
            Item { Layout.fillWidth: true }
            BusyIndicator {
                visible: root.controller.teamsLoading
                running: visible
                implicitWidth: 18
                implicitHeight: 18
            }
            Button {
                text: "+ New"
                implicitHeight: 28
                onClicked: createDialog.open()
            }
            ToolButton {
                text: "↻"
                implicitWidth: 28
                implicitHeight: 28
                onClicked: {
                    if (root.controller.selectedTeamId.length > 0)
                        root.controller.selectTeam(root.controller.selectedTeamId);
                    else
                        root.controller.loadTeams();
                }
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#303347" }

        Rectangle {
            visible: root.controller.teamsError.length > 0
            Layout.fillWidth: true
            implicitHeight: visible ? 34 : 0
            color: "#2b2028"
            Text {
                anchors.fill: parent
                anchors.margins: 8
                text: root.controller.teamsError
                color: "#c98a98"
                font.pixelSize: 12
                elide: Text.ElideRight
            }
        }

        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal

            ListView {
                id: teamList
                SplitView.preferredWidth: 280
                SplitView.minimumWidth: 190
                SplitView.maximumWidth: 390
                model: root.controller.teams
                clip: true
                spacing: 4
                topMargin: 8
                bottomMargin: 8
                leftMargin: 8
                rightMargin: 8

                delegate: ItemDelegate {
                    id: teamRow
                    required property var modelData
                    width: ListView.view.width - 16
                    height: 62
                    highlighted: String(modelData.team_id || "")
                        === root.controller.selectedTeamId
                    onClicked: root.controller.selectTeam(String(modelData.team_id || ""))
                    background: Rectangle {
                        radius: 5
                        color: teamRow.highlighted ? "#272b40"
                            : teamRow.hovered ? "#202332" : "transparent"
                        border.color: teamRow.highlighted ? "#596083" : "transparent"
                    }
                    contentItem: RowLayout {
                        spacing: 9
                        Rectangle {
                            Layout.preferredWidth: 34
                            Layout.preferredHeight: 34
                            radius: 9
                            color: {
                                const value = String(teamRow.modelData.color || "");
                                return value.startsWith("#") ? value : "#41465f";
                            }
                            Text {
                                anchors.centerIn: parent
                                text: String(teamRow.modelData.name || "?").slice(0, 1).toUpperCase()
                                color: "#eef0fb"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            RowLayout {
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: String(teamRow.modelData.name || "Team")
                                    color: "#c8cadc"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Rectangle {
                                    visible: Number(teamRow.modelData.unread_count || 0) > 0
                                    Layout.preferredWidth: 18
                                    Layout.preferredHeight: 16
                                    radius: 8
                                    color: "#91a884"
                                    Text {
                                        anchors.centerIn: parent
                                        text: String(teamRow.modelData.unread_count || 0)
                                        color: "#171923"
                                        font.pixelSize: 11
                                    }
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: String(teamRow.modelData.latest_message || "")
                                    || String((teamRow.modelData.member_agent_ids || []).length)
                                        + " members"
                                color: "#6c7188"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }

            Rectangle {
                SplitView.fillWidth: true
                color: "#191b27"

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        color: "#1e2130"
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 10
                            spacing: 9
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    Layout.fillWidth: true
                                    text: {
                                        const team = root.selectedTeam();
                                        return team ? String(team.name || "Team") : "Select a team";
                                    }
                                    color: "#d0d2e3"
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: {
                                        const team = root.selectedTeam();
                                        if (!team)
                                            return "";
                                        const members = (team.member_agent_ids || []).length;
                                        const leader = String(team.leader_agent_id || "");
                                        return members + " members"
                                            + (leader.length > 0 ? "  ·  leader "
                                                + root.controller.agentNameById(leader) : "");
                                    }
                                    color: "#686d85"
                                    font.family: "JetBrains Mono"
                                    font.pixelSize: 11
                                    elide: Text.ElideMiddle
                                }
                            }
                            ToolButton {
                                visible: root.controller.selectedTeamId.length > 0
                                text: "···"
                                implicitWidth: 28
                                implicitHeight: 28
                                onClicked: teamMenu.open()
                                Menu {
                                    id: teamMenu
                                    MenuItem {
                                        text: "Edit team"
                                        onTriggered: {
                                            const team = root.selectedTeam();
                                            if (!team)
                                                return;
                                            editName.text = String(team.name || "");
                                            editColor.text = String(team.color || "");
                                            const choices = root.teamLeaderChoices();
                                            const leader = String(team.leader_agent_id || "");
                                            let index = 0;
                                            for (let i = 0; i < choices.length; ++i) {
                                                if (String(choices[i].id || "") === leader) {
                                                    index = i;
                                                    break;
                                                }
                                            }
                                            editLeader.currentIndex = index;
                                            editDialog.open();
                                        }
                                    }
                                    MenuItem {
                                        text: {
                                            const team = root.selectedTeam();
                                            return team && Boolean(team.nudge_enabled)
                                                ? "Disable leader nudging"
                                                : "Enable leader nudging";
                                        }
                                        onTriggered: {
                                            const team = root.selectedTeam();
                                            if (team)
                                                root.controller.setTeamNudging(
                                                    root.controller.selectedTeamId,
                                                    !Boolean(team.nudge_enabled));
                                        }
                                    }
                                    MenuItem {
                                        text: "Delete team…"
                                        onTriggered: deleteDialog.open()
                                    }
                                }
                                ToolTip.visible: hovered
                                ToolTip.text: "Team actions"
                            }
                        }
                    }

                    Rectangle {
                        visible: root.controller.selectedTeamId.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: visible ? 42 : 0
                        color: "#1b1e2a"
                        border.color: "#292d3f"

                        Flickable {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 10
                            contentWidth: memberRow.implicitWidth
                            contentHeight: height
                            clip: true
                            boundsBehavior: Flickable.StopAtBounds

                            Row {
                                id: memberRow
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: 6

                                Repeater {
                                    model: {
                                        const team = root.selectedTeam();
                                        return team ? (team.member_agent_ids || []) : [];
                                    }
                                    delegate: Rectangle {
                                        id: memberChip
                                        required property string modelData
                                        implicitWidth: memberName.implicitWidth + removeMember.implicitWidth + 18
                                        implicitHeight: 25
                                        radius: 12
                                        color: "#272b3c"
                                        border.color: "#3b4057"
                                        Row {
                                            anchors.centerIn: parent
                                            spacing: 4
                                            Text {
                                                id: memberName
                                                text: root.controller.agentNameById(memberChip.modelData)
                                                color: "#aeb2cb"
                                                font.pixelSize: 11
                                            }
                                            ToolButton {
                                                id: removeMember
                                                text: "×"
                                                implicitWidth: 18
                                                implicitHeight: 18
                                                onClicked: root.controller.removeTeamMember(
                                                    root.controller.selectedTeamId,
                                                    memberChip.modelData)
                                                ToolTip.visible: hovered
                                                ToolTip.text: "Remove member"
                                            }
                                        }
                                    }
                                }

                                Button {
                                    text: "+ member"
                                    implicitHeight: 25
                                    onClicked: {
                                        memberChoice.currentIndex = 0;
                                        memberDialog.open();
                                    }
                                }
                            }
                        }
                    }

                    ListView {
                        id: messages
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        model: root.controller.teamMessages
                        clip: true
                        spacing: 7
                        leftMargin: 14
                        rightMargin: 14
                        topMargin: 12
                        bottomMargin: 18
                        reuseItems: true

                        delegate: Rectangle {
                            id: message
                            required property var modelData
                            width: ListView.view.width - 28
                            implicitHeight: messageColumn.implicitHeight + 18
                            radius: 5
                            color: "#202331"
                            border.width: 0
                            ColumnLayout {
                                id: messageColumn
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 9
                                spacing: 5
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        Layout.fillWidth: true
                                        text: String(message.modelData.source_name || "Agent")
                                        color: "#9ca3c7"
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                    }
                                    Button {
                                        visible: String(message.modelData.source_session || "").length > 0
                                        text: "Open chat"
                                        implicitHeight: 24
                                        onClicked: root.openChat(
                                            String(message.modelData.source_session || ""))
                                    }
                                }
                                TextEdit {
                                    Layout.fillWidth: true
                                    readOnly: true
                                    selectByMouse: true
                                    wrapMode: TextEdit.Wrap
                                    text: String(message.modelData.text || "")
                                    color: "#b8bbcf"
                                    font.pixelSize: 13
                                }
                            }
                        }

                        Label {
                            anchors.centerIn: parent
                            visible: messages.count === 0 && !root.controller.teamsLoading
                            text: root.controller.selectedTeamId.length > 0
                                ? "No team messages yet" : "Choose a team"
                            color: "#62677f"
                            font.pixelSize: 12
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: createDialog
        anchors.centerIn: parent
        modal: true
        title: "Create team"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            root.controller.createTeam(teamName.text);
            teamName.clear();
        }
        ColumnLayout {
            width: 320
            Label { text: "Team name" }
            TextField {
                id: teamName
                Layout.fillWidth: true
                placeholderText: "e.g. Release crew"
            }
        }
    }

    Dialog {
        id: deleteDialog
        anchors.centerIn: parent
        modal: true
        title: "Delete team?"
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: root.controller.deleteTeam(root.controller.selectedTeamId)
        Label {
            text: "This removes the team and its history."
            color: "#b8bbcf"
        }
    }

    Dialog {
        id: editDialog
        anchors.centerIn: parent
        modal: true
        title: "Edit team"
        standardButtons: Dialog.Save | Dialog.Cancel
        onAccepted: root.controller.updateTeam(
            root.controller.selectedTeamId,
            editName.text,
            editColor.text,
            String(editLeader.currentValue || ""))
        ColumnLayout {
            width: 340
            Label { text: "Name" }
            TextField { id: editName; Layout.fillWidth: true }
            Label { text: "Color" }
            TextField {
                id: editColor
                Layout.fillWidth: true
                placeholderText: "#596083"
            }
            Label { text: "Leader" }
            ComboBox {
                id: editLeader
                Layout.fillWidth: true
                model: root.teamLeaderChoices()
                textRole: "name"
                valueRole: "id"
            }
        }
    }

    Dialog {
        id: memberDialog
        anchors.centerIn: parent
        modal: true
        title: "Add team member"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            if (memberChoice.currentIndex >= 0)
                root.controller.addTeamMember(
                    root.controller.selectedTeamId,
                    String(memberChoice.currentValue || ""));
        }
        ColumnLayout {
            width: 320
            Label { text: "Agent" }
            ComboBox {
                id: memberChoice
                Layout.fillWidth: true
                model: root.controller.teamAgentChoices()
                textRole: "name"
                valueRole: "id"
            }
        }
    }

    Timer {
        running: root.visible && root.controller.selectedTeamId.length > 0
        repeat: true
        interval: 10_000
        onTriggered: root.controller.selectTeam(root.controller.selectedTeamId)
    }

    onVisibleChanged: {
        if (visible)
            controller.loadTeams();
    }
}
