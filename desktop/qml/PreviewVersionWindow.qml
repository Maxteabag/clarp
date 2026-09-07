import QtQuick
import QtQuick.Controls
import "components"

ApplicationWindow {
    width: 660
    height: 560
    visible: true
    title: "Clarp preview versions"
    color: "#191b26"
    palette.window: "#1a1b26"
    palette.base: "#1a1b26"
    palette.alternateBase: "#20212e"
    palette.button: "#292b3a"
    palette.toolTipBase: "#292b3a"
    palette.windowText: "#c0caf5"
    palette.text: "#c0caf5"
    palette.buttonText: "#c0caf5"
    palette.toolTipText: "#c0caf5"
    palette.brightText: "#1a1b26"
    palette.placeholderText: "#8d93b0"
    palette.highlight: "#bb9af7"
    palette.accent: "#bb9af7"
    palette.highlightedText: "#1a1b26"
    palette.link: "#7aa2f7"
    palette.linkVisited: "#bb9af7"
    palette.light: "#565b76"
    palette.midlight: "#41445a"
    palette.mid: "#41445a"
    palette.dark: "#bb9af7"
    palette.shadow: "#14151d"
    palette.disabled.text: "#8d93b0"
    palette.disabled.windowText: "#8d93b0"
    palette.disabled.buttonText: "#8d93b0"
    PreviewVersions { id: versions }
    PreviewVersionPanel {
        anchors.fill: parent
        switcher: versions
        visible: true
        onVisibleChanged: if (!visible) Qt.quit()
    }
}
