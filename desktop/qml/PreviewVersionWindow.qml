import QtQuick
import QtQuick.Controls
import "components"

ApplicationWindow {
    width: 660
    height: 560
    visible: true
    title: "Clarp preview versions"
    color: Theme.raised
    palette.window: Theme.window
    palette.base: Theme.window
    palette.alternateBase: Theme.raised
    palette.button: Theme.control
    palette.toolTipBase: Theme.control
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.buttonText: Theme.text
    palette.toolTipText: Theme.text
    palette.brightText: Theme.window
    palette.placeholderText: Theme.muted
    palette.highlight: Theme.accent
    palette.accent: Theme.accent
    palette.highlightedText: Theme.window
    palette.link: Theme.link
    palette.linkVisited: Theme.accent
    palette.light: "#565b76"
    palette.midlight: Theme.border
    palette.mid: Theme.border
    palette.dark: Theme.accent
    palette.shadow: Theme.shadow
    palette.disabled.text: Theme.muted
    palette.disabled.windowText: Theme.muted
    palette.disabled.buttonText: Theme.muted
    PreviewVersions { id: versions }
    PreviewVersionPanel {
        anchors.fill: parent
        switcher: versions
        visible: true
        onVisibleChanged: if (!visible) Qt.quit()
    }
}
