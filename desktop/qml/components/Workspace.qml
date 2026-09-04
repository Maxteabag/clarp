pragma ComponentBehavior: Bound

import QtQuick

Rectangle {
    id: root

    required property var controller
    signal openConnectionRequested
    color: "#121116"

    Repeater {
        model: root.controller.panes.paneLayout

        PaneLeaf {
            required property var modelData
            x: Math.round(Number(modelData.x) * root.width)
            y: Math.round(Number(modelData.y) * root.height)
            width: Math.round(Number(modelData.width) * root.width)
            height: Math.round(Number(modelData.height) * root.height)
            controller: root.controller
            node: modelData
            onOpenConnectionRequested: root.openConnectionRequested()
        }
    }

    Repeater {
        model: root.controller.panes.splitLayout

        Rectangle {
            id: splitHandle

            required property var modelData
            readonly property bool vertical: String(modelData.direction) === "vertical"
            readonly property real splitX: Number(modelData.x) * root.width
            readonly property real splitY: Number(modelData.y) * root.height
            readonly property real splitWidth: Number(modelData.width) * root.width
            readonly property real splitHeight: Number(modelData.height) * root.height

            x: vertical ? splitX + splitWidth * Number(modelData.ratio) - width / 2 : splitX
            y: vertical ? splitY : splitY + splitHeight * Number(modelData.ratio) - height / 2
            width: vertical ? 7 : splitWidth
            height: vertical ? splitHeight : 7
            color: dragHandler.active ? "#b884d8" : hoverHandler.hovered ? "#5d4969" : "#29242f"
            z: 10

            HoverHandler {
                id: hoverHandler
            }
            DragHandler {
                id: dragHandler
                xAxis.enabled: splitHandle.vertical
                yAxis.enabled: !splitHandle.vertical
                xAxis.minimum: splitHandle.splitX + splitHandle.splitWidth * 0.15
                xAxis.maximum: splitHandle.splitX + splitHandle.splitWidth * 0.85
                yAxis.minimum: splitHandle.splitY + splitHandle.splitHeight * 0.15
                yAxis.maximum: splitHandle.splitY + splitHandle.splitHeight * 0.85
                onActiveChanged: {
                    if (active)
                        return;
                    const ratio = splitHandle.vertical ? (splitHandle.x + splitHandle.width / 2 - splitHandle.splitX) / splitHandle.splitWidth : (splitHandle.y + splitHandle.height / 2 - splitHandle.splitY) / splitHandle.splitHeight;
                    root.controller.panes.setSplitRatio(String(splitHandle.modelData.id), ratio);
                }
            }
            TapHandler {
                acceptedButtons: Qt.LeftButton
                onDoubleTapped: root.controller.panes.equalize()
            }
        }
    }
}
