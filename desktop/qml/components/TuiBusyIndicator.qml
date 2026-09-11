import QtQuick
import QtQuick.Controls
BusyIndicator {
    id: control
    implicitWidth: 26
    implicitHeight: 26
    font.family: "JetBrains Mono"
    contentItem: Text {
        id: glyph
        property int frame: 0
        readonly property var frames: ["|", "/", "-", "\\"]
        text: frames[frame]
        visible: control.running
        font: control.font
        color: control.palette.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        Timer {
            interval: 150
            repeat: true
            running: control.running && control.visible
            onTriggered: glyph.frame = (glyph.frame + 1) % glyph.frames.length
        }
    }
}
