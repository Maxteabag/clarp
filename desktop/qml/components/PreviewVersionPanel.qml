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
    Rectangle { anchors.fill: parent; color: Qt.alpha(Theme.sunken, 0.73) }
    MouseArea { anchors.fill: parent; onClicked: root.close() }
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(620, parent.width - 32)
        height: Math.min(520, parent.height - 32)
        color: Theme.control
        border.color: Theme.faint
        radius: 0
        MouseArea { anchors.fill: parent }
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 12
            TuiLabel { text: "Preview versions"; font.pixelSize: 17; color: Theme.text }
            TuiLabel {
                Layout.fillWidth: true
                text: "Choose a build and reopen. Rolling back pauses automatic updates. Chats stay on the Host."
                wrapMode: Text.Wrap
                color: Theme.text
            }
            TuiLabel {
                visible: !root.canRestart
                text: "Finish sending, uploads, recording, transcription or playback before switching versions."
                color: Theme.warning
            }
            TuiLabel {
                visible: Boolean(root.switcher.catalog.pinned)
                text: "Pinned to an older build — automatic updates paused"
                color: Theme.warning
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
                    palette.buttonText: Theme.text
                    background: Rectangle {
                        color: versionRow.hovered || versionRow.activeFocus
                            || (versionRow.ListView.isCurrentItem && versions.activeFocus) ? Theme.border : Theme.border
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
                color: Theme.danger
            }
            TuiLabel {
                Layout.fillWidth: true
                visible: String(root.switcher.notice || "").length > 0
                text: String(root.switcher.notice || "")
                wrapMode: Text.Wrap
                color: Theme.success
            }
            TuiLabel {
                Layout.fillWidth: true
                text: "Recovery is always available from Clarp Preview Versions in the app launcher."
                wrapMode: Text.Wrap
                color: Theme.secondary
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
