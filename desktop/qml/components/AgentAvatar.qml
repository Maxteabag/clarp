import QtQuick

Item {
    id: root

    required property var controller
    required property string session
    required property string name
    property string symbol: ""
    property real avatarSize: 28
    property real cornerRadius: 7
    property color fallbackColor: "#55596f"
    readonly property url resolvedSource: {
        root.controller.avatarRevision;
        return root.session.length > 0 ? root.controller.avatarSource(root.session) : "";
    }

    implicitWidth: avatarSize
    implicitHeight: avatarSize

    Rectangle {
        anchors.fill: parent
        radius: root.cornerRadius
        clip: true
        color: root.fallbackColor
        border.width: 1
        border.color: "#353749"

        Text {
            anchors.centerIn: parent
            text: root.symbol.length > 0 ? root.symbol : root.name.slice(0, 1).toUpperCase()
            color: "#e0e1ec"
            font.family: "JetBrains Mono"
            font.pixelSize: Math.max(9, root.avatarSize * 0.38)
            font.weight: Font.DemiBold
        }

        Image {
            anchors.fill: parent
            source: root.resolvedSource
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: true
            visible: status === Image.Ready
        }
    }
}
