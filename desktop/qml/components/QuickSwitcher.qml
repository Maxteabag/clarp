pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    property string query: ""
    readonly property var results: {
        controller.agentRevision;
        return controller.matchingAgents(query);
    }
    color: "#c0121116"

    function open() {
        query = "";
        visible = true;
        search.forceActiveFocus();
    }

    function choose(index) {
        if (index < 0 || index >= results.length)
            return;
        controller.selectSession(String(results[index].session));
        visible = false;
    }

    MouseArea {
        anchors.fill: parent
        onClicked: root.visible = false
    }

    Rectangle {
        width: Math.min(620, parent.width - 60)
        height: Math.min(520, parent.height - 90)
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: 70
        radius: 18
        color: "#1b1820"
        border.color: "#4a3a53"

        MouseArea {
            anchors.fill: parent
            onClicked: mouse => mouse.accepted = true
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 10

            TextField {
                id: search
                Layout.fillWidth: true
                text: root.query
                placeholderText: "Go to agent…"
                font.pixelSize: 15
                onTextChanged: {
                    root.query = text;
                    resultList.currentIndex = root.results.length > 0 ? 0 : -1;
                }
                Keys.onPressed: event => {
                    if (event.key === Qt.Key_Down) {
                        resultList.currentIndex = Math.min(root.results.length - 1, resultList.currentIndex + 1);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Up) {
                        resultList.currentIndex = Math.max(0, resultList.currentIndex - 1);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                        root.choose(resultList.currentIndex);
                        event.accepted = true;
                    } else if (event.key === Qt.Key_Escape) {
                        root.visible = false;
                        event.accepted = true;
                    }
                }
            }

            ListView {
                id: resultList
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: root.results
                spacing: 5
                clip: true
                currentIndex: root.results.length > 0 ? 0 : -1

                delegate: ItemDelegate {
                    id: resultRow
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 58
                    highlighted: ListView.isCurrentItem
                    hoverEnabled: true
                    onHoveredChanged: {
                        if (hovered)
                            resultList.currentIndex = index;
                    }
                    onClicked: root.choose(index)

                    background: Rectangle {
                        radius: 11
                        color: resultRow.highlighted ? "#30263a" : resultRow.hovered ? "#24202a" : "transparent"
                    }
                    contentItem: RowLayout {
                        spacing: 11
                        Rectangle {
                            Layout.preferredWidth: 34
                            Layout.preferredHeight: 34
                            radius: 10
                            color: resultRow.modelData.busy ? "#674a36" : "#3c3045"
                            Text {
                                anchors.centerIn: parent
                                text: String(resultRow.modelData.name).slice(0, 1).toUpperCase()
                                color: "#f1e9f4"
                                font.weight: Font.DemiBold
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                text: String(resultRow.modelData.name)
                                color: "#eee8e2"
                                font.pixelSize: 13
                                font.weight: Font.Medium
                            }
                            Text {
                                text: String(resultRow.modelData.backend) + "  ·  " + String(resultRow.modelData.session)
                                color: "#77717f"
                                font.pixelSize: 10
                            }
                        }
                        StatusPill {
                            status: String(resultRow.modelData.state)
                        }
                    }
                }
            }

            Text {
                visible: root.results.length === 0
                Layout.alignment: Qt.AlignHCenter
                text: "No matching agent"
                color: "#756e7c"
                font.pixelSize: 12
            }
        }
    }
}
