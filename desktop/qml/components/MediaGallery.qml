pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller
    required property string session
    property bool compact: true
    property bool expanded: false
    readonly property int mediaRevision: controller.mediaRevision
    readonly property var assets: {
        mediaRevision;
        return controller.mediaForSession(session);
    }
    readonly property bool showing: assets.length > 0 && (!compact || expanded)

    visible: assets.length > 0
    implicitHeight: !visible ? 0 : compact && !expanded ? 27 : compact ? 116 : 224
    color: "#181a24"
    border.color: "#303448"
    radius: 5

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 18
            Text {
                Layout.fillWidth: true
                text: "MEDIA  " + root.assets.length
                color: "#858aa5"
                font.family: "JetBrains Mono"
                font.pixelSize: 8
                font.weight: Font.DemiBold
                font.letterSpacing: 0.8
            }
            ToolButton {
                visible: root.compact
                text: root.expanded ? "−" : "+"
                implicitWidth: 20
                implicitHeight: 18
                onClicked: root.expanded = !root.expanded
                ToolTip.visible: hovered
                ToolTip.text: root.expanded ? "Collapse media" : "Show recent media"
            }
        }

        ListView {
            id: gallery
            visible: root.showing
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: ListView.Horizontal
            model: root.assets
            spacing: 7
            clip: true
            reuseItems: true

            delegate: Rectangle {
                id: mediaCard
                required property var modelData
                width: root.compact ? 142 : 190
                height: ListView.view.height
                radius: 4
                color: "#11131b"
                border.color: "#34394e"
                clip: true

                Image {
                    id: preview
                    anchors.fill: parent
                    source: root.controller.mediaSource(
                        String(mediaCard.modelData.asset_id || ""))
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: true
                }
                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: caption.implicitHeight + 10
                    color: "#c012141d"
                    Text {
                        id: caption
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 6
                        anchors.rightMargin: 6
                        text: String(mediaCard.modelData.caption
                            || mediaCard.modelData.source_name || "Image")
                        color: "#e0e2ed"
                        font.pixelSize: 9
                        elide: Text.ElideRight
                    }
                }
                BusyIndicator {
                    anchors.centerIn: parent
                    running: preview.status === Image.Loading
                    visible: running
                    implicitWidth: 20
                    implicitHeight: 20
                }
                TapHandler {
                    enabled: preview.status === Image.Ready
                    onTapped: {
                        viewerImage.source = preview.source;
                        viewerCaption.text = String(mediaCard.modelData.caption
                            || mediaCard.modelData.source_name || "Image");
                        viewer.open();
                    }
                }
            }

            ScrollBar.horizontal: ScrollBar {}
        }
    }

    Popup {
        id: viewer
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 1100)
        height: Math.min(parent.height - 48, 820)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {
            radius: 7
            color: "#11131b"
            border.color: "#555c7d"
        }
        contentItem: Item {
            Image {
                id: viewerImage
                anchors.fill: parent
                anchors.margins: 10
                anchors.bottomMargin: 36
                fillMode: Image.PreserveAspectFit
                asynchronous: true
            }
            Text {
                id: viewerCaption
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 10
                color: "#c4c8da"
                font.pixelSize: 10
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }
}
