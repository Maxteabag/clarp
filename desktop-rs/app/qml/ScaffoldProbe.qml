import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    id: root
    width: 320; height: 120; visible: true
    title: "clarp-desktop (rust)"
    AppController { id: controller }
    Text { anchors.centerIn: parent; text: "Rust controller: " + controller.baseUrl }
    Component.onCompleted: {
        console.log("SCAFFOLD_OK " + controller.baseUrl + " connected=" + controller.connected)
        if (Qt.application.arguments.indexOf("--probe-exit") >= 0) Qt.quit()
    }
}
