import QtQuick
import QtQuick.Controls
import "components"

ApplicationWindow {
    width: 660
    height: 560
    visible: true
    title: "Clarp preview versions"
    color: "#191b26"
    PreviewVersions { id: versions }
    PreviewVersionPanel {
        anchors.fill: parent
        switcher: versions
        visible: true
        onVisibleChanged: if (!visible) Qt.quit()
    }
}
