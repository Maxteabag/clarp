import QtQuick
Item {
 id: root
 property bool working: false
 property bool reducedMotion: false
 property real phase: 0
 clip: true
 visible: working && !reducedMotion
 Rectangle {
  width: parent.width * 0.3; height: parent.height
  x: -width + Math.max(0,(root.phase-0.05)/0.95)*(parent.width+width)
  color: "#9ece6a"; opacity: 0.12
 }
}
