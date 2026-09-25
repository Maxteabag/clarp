pragma ComponentBehavior: Bound
import QtQuick
Rectangle {
    id: root
    objectName: "replyTypingIndicator"
    implicitWidth: 132
    implicitHeight: 32
    radius: 0
    color: Theme.window
    Accessible.role: Accessible.StaticText
    Accessible.name: "Agent is working"
    Row {
        id: indicatorRow
        anchors.centerIn: parent
        spacing: 5
        TuiText {
            text: "Working…"
            color: Theme.text
            font.pixelSize: 12
            anchors.verticalCenter: indicatorRow.verticalCenter
        }
        Repeater {
            model: 3
            Rectangle {
                id: dot
                required property int index
                width: 6; height: 6; radius: 0
                anchors.verticalCenter: indicatorRow.verticalCenter
                color: Theme.accent
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
