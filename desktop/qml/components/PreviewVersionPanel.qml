pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FocusScope {
    id: root
    required property var switcher
    property bool canRestart: true
    signal closed()
    function close() { visible = false; closed(); }
    visible: false
    onVisibleChanged: if (visible) { forceActiveFocus(); switcher.refresh(); }
    Keys.onEscapePressed: close()
    Rectangle { anchors.fill: parent; color: "#bb101018" }
    MouseArea { anchors.fill: parent; onClicked: root.close() }
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(620, parent.width - 32)
        height: Math.min(520, parent.height - 32)
        color: "#1e2130"
        border.color: "#42445b"
        radius: 0
        MouseArea { anchors.fill: parent }
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 12
            TuiLabel { text: "Preview versions"; font.pixelSize: 17; color: "#c5c7e2" }
            TuiLabel {
                Layout.fillWidth: true
                text: "Choose a build and reopen. Rolling back pauses automatic updates. Chats stay on the Host."
                wrapMode: Text.Wrap
                color: "#a7abc5"
            }
            TuiLabel {
                visible: !root.canRestart
                text: "Finish sending, uploads, recording, transcription or playback before switching versions."
                color: "#e2b979"
            }
            TuiLabel {
                visible: Boolean(root.switcher.catalog.pinned)
                text: "Pinned to an older build — automatic updates paused"
                color: "#e2b979"
            }
            ListView {
                id: versions
                objectName: "previewVersionList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 4
                focus: true
                keyNavigationEnabled: true
                model: root.switcher.catalog.versions || []
                Keys.onReturnPressed: {
                    if (root.canRestart && !root.switcher.busy && currentIndex >= 0 && currentIndex < count)
                        root.switcher.selectVersion(String(model[currentIndex].hash));
                }
                ScrollBar.vertical: ScrollBar { }
                delegate: ItemDelegate {
                    id: versionRow
                    objectName: "previewVersion-" + String(modelData.hash)
                    required property var modelData
                    width: versions.width
                    enabled: !root.switcher.busy && root.canRestart
                    text: String(modelData.label)
                        + (modelData.hash === root.switcher.runningHash ? "   · running" : "")
                        + (modelData.hash === root.switcher.catalog.current ? "   · installed" : "")
                        + (modelData.hash === root.switcher.catalog.latest ? "   · latest" : "")
                    palette.buttonText: "#c5c7e2"
                    background: Rectangle {
                        color: versionRow.hovered || versionRow.activeFocus
                            || (versionRow.ListView.isCurrentItem && versions.activeFocus) ? "#34364b" : "#272a3b"
                        radius: 0
                    }
                    onClicked: root.switcher.selectVersion(String(modelData.hash))
                    Keys.onReturnPressed: root.switcher.selectVersion(String(modelData.hash))
                    Keys.onEnterPressed: root.switcher.selectVersion(String(modelData.hash))
                }
            }
            TuiLabel {
                Layout.fillWidth: true
                visible: root.switcher.error.length > 0
                text: root.switcher.error
                wrapMode: Text.Wrap
                color: "#e79aa4"
            }
            TuiLabel {
                Layout.fillWidth: true
                visible: String(root.switcher.notice || "").length > 0
                text: String(root.switcher.notice || "")
                wrapMode: Text.Wrap
                color: "#a5cda0"
            }
            TuiLabel {
                Layout.fillWidth: true
                text: "Recovery is always available from Clarp Preview Versions in the app launcher."
                wrapMode: Text.Wrap
                color: "#8f94b0"
                font.pixelSize: 11
            }
            RowLayout {
                TuiButton {
                    text: "Latest · resume updates"
                    enabled: !root.switcher.busy && root.canRestart
                    onClicked: root.switcher.selectVersion("latest")
                }
                Item { Layout.fillWidth: true }
                TuiButton { text: "Close · Esc"; onClicked: root.close() }
            }
        }
    }
}
