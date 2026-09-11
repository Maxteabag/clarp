pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    required property var keymap
    objectName: "contextShortcutBar"
    implicitHeight: Math.max(30, hints.implicitHeight + 12)
    color: "#171822"
    Rectangle { width: parent.width; height: 1; color: "#303247" }
    RowLayout {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 16
        TuiText {
            text: root.keymap.contextName === "composer" ? "INSERT"
                : root.keymap.contextName === "sidebar" ? "AGENTS"
                : root.keymap.contextName === "pane" ? "CONVERSATION"
                : root.keymap.contextName.toUpperCase()
            color: "#bb9af7"
            font.pixelSize: 10
            font.weight: Font.DemiBold
        }
        Flow {
            id: hints
            Layout.fillWidth: true
            spacing: 16
            Repeater {
                model: root.keymap.hints
                delegate: Row {
                    id: hint
                    required property var modelData
                    spacing: 5
                    TuiText {
                        text: hint.modelData.label + ":"
                        color: "#9ca1bd"
                        font.pixelSize: 11
                    }
                    TuiText {
                        text: hint.modelData.keys[0].replace("Return", "Enter").replace("Escape", "Esc")
                        color: "#c7adf1"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
            }
        }
    }
}
