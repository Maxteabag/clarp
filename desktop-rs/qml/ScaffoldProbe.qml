import QtQuick
import QtQuick.Window
import Clarp.Native

Window {
    id: root
    width: 320; height: 120; visible: true
    title: "clarp-desktop (rust scaffold)"
    AppController { id: controller; serverUrl: "http://127.0.0.1:0" }
    Text { anchors.centerIn: parent; text: "Rust controller: " + controller.serverUrl }
    Component.onCompleted: {
        console.log("SCAFFOLD_OK " + controller.serverUrl + " connected=" + controller.connected)
        if (Qt.application.arguments.indexOf("--probe-exit") >= 0) Qt.quit()
    }
}
