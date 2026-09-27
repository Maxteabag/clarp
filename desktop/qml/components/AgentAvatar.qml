import QtQuick

Item {
    id: root

    required property var controller
    required property string session
    required property string name
    property bool showPortrait: false
    // Rows off screen pass loadPortrait: false so they do no portrait work.
    // Once a row has been on screen its portrait stays, so scrolling back
    // does not flash the letter placeholder while the image reloads.
    property bool loadPortrait: true
    property bool portraitWanted: false
    onLoadPortraitChanged: if (loadPortrait) portraitWanted = true
    Component.onCompleted: if (loadPortrait) portraitWanted = true
    // An explicit portrait (a contact's persona avatar) wins over the
    // session lookup; an empty value keeps the agent-avatar path.
    property url portraitSource: ""
    property string contactName: ""
    property string symbol: ""
    property real avatarSize: 40
    property real cornerRadius: 2
    property color fallbackColor: Theme.faint
    property bool firstFramePassed: false
    readonly property url resolvedSource: {
        root.controller.avatarRevision;
        if (!root.showPortrait || !root.portraitWanted || !root.firstFramePassed) return "";
        if (String(root.portraitSource).length > 0) return root.portraitSource;
        if (root.contactName.length > 0 && typeof root.controller.contactAvatarSource === "function")
            return root.controller.contactAvatarSource(root.contactName);
        return root.session.length > 0 ? root.controller.avatarSource(root.session) : "";
    }

    implicitWidth: avatarSize
    implicitHeight: avatarSize
    clip: true

    Timer {
        interval: 0
        running: true
        repeat: false
        onTriggered: root.firstFramePassed = true
    }

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
