import QtQuick

Item {
    id: root

    required property var controller
    required property string session
    required property string name
    property bool showPortrait: false
    // An explicit portrait (a contact's persona avatar) wins over the
    // session lookup; an empty value keeps the agent-avatar path.
    property url portraitSource: ""
    property string symbol: ""
    property real avatarSize: 40
    property real cornerRadius: 2
    property color fallbackColor: Theme.faint
    readonly property url resolvedSource: {
        root.controller.avatarRevision;
        if (!root.showPortrait) return "";
        if (String(root.portraitSource).length > 0) return root.portraitSource;
        return root.session.length > 0 ? root.controller.avatarSource(root.session) : "";
    }

    implicitWidth: avatarSize
    implicitHeight: avatarSize
    clip: true

    Rectangle {
        anchors.fill: parent
        visible: portrait.status !== Image.Ready
        radius: root.cornerRadius
        antialiasing: true
        color: "transparent"
        border.width: 1
        border.color: Theme.border

        TuiText {
            anchors.centerIn: parent
            text: root.symbol.length > 0 ? root.symbol : root.name.slice(0, 1).toUpperCase()
            color: Theme.text
            font.family: "JetBrains Mono"
            font.pixelSize: Math.max(9, root.avatarSize * 0.38)
            font.weight: Font.DemiBold
        }

    }

    Image {
        id: portrait
        anchors.fill: parent
        source: root.resolvedSource
        // Decode above the displayed pixel size, then filter down. This also
        // covers the application's fractional UI scale on high-DPI screens.
        sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio * 2),
                            Math.ceil(height * Screen.devicePixelRatio * 2))
        fillMode: Image.PreserveAspectCrop
        smooth: true
        mipmap: true
        asynchronous: true
        cache: true
        visible: status === Image.Ready
    }
}
