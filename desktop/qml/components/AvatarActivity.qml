import QtQuick

// Native wrapper: caller supplies authoritative state and common episode time.
Item {
    id: root
    required property var controller
    required property string session
    required property string name
    property bool working: false
    property bool reducedMotion: motionClock.reducedMotion
    property real avatarSize: 40
    property real cornerRadius: 2
    property color fallbackColor: "#55596f"
    property string symbol: ""
    property bool showPortrait: false
    readonly property var motionClock: controller.avatarMotion
    readonly property bool authoritativeWorking: { motionClock.revision; return motionClock.working(session); }
    property real phase: { motionClock.revision; return motionClock.phase(session); }
    function updateObservation() { motionClock.observe(root, root.visible && root.working && root.authoritativeWorking); }
    Component.onCompleted: updateObservation()
    Component.onDestruction: motionClock.observe(root, false)
    onVisibleChanged: updateObservation()
    onWorkingChanged: updateObservation()
    onAuthoritativeWorkingChanged: updateObservation()
    implicitWidth: avatarSize
    implicitHeight: avatarSize
    AgentAvatar { anchors.fill: parent; controller: root.controller; session: root.session; name: root.name
        avatarSize: root.avatarSize; cornerRadius: root.cornerRadius; fallbackColor: root.fallbackColor
        symbol: root.symbol; showPortrait: root.showPortrait }
    Rectangle { anchors.fill: parent; anchors.margins: -3; color: "transparent"; border.width: 2
        border.color: "#9ece6a"; radius: root.cornerRadius + 3; visible: root.working && root.authoritativeWorking
        opacity: root.reducedMotion ? 0.6 : 0.35 + 0.65 * (1 + Math.cos(root.phase * Math.PI * 2)) / 2
        scale: root.reducedMotion ? 1 : 1 + 0.035 * (1 + Math.cos(root.phase * Math.PI * 2)) / 2 }
}
