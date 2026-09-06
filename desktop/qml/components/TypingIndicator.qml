pragma ComponentBehavior: Bound
import QtQuick
Rectangle {
    id: root
    objectName: "replyTypingIndicator"
    implicitWidth: 132
    implicitHeight: 32
    radius: 16
    color: "#1b191f"
    Accessible.role: Accessible.StaticText
    Accessible.name: "Agent is working"
    Row {
        anchors.centerIn: parent
        spacing: 5
        Text {
            text: "Working…"
            color: "#9ea4c7"
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
        }
        Repeater {
            model: 3
            Rectangle {
                id: dot
                required property int index
                width: 6; height: 6; radius: 3
                anchors.verticalCenter: parent.verticalCenter
                color: "#bb9af7"
                opacity: 0.4
                SequentialAnimation on opacity {
                    running: root.visible
                    loops: Animation.Infinite
                    PauseAnimation { duration: dot.index * 140 }
                    NumberAnimation { to: 1; duration: 260 }
                    NumberAnimation { to: 0.4; duration: 260 }
                    PauseAnimation { duration: (2 - dot.index) * 140 }
                }
            }
        }
    }
}
