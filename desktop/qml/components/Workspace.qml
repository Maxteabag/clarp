pragma ComponentBehavior: Bound

import QtQuick

Rectangle {
    id: root

    required property var controller
    signal openConnectionRequested
    signal queueRequested(string session)
    signal profileRequested(string session)
    readonly property real paneGap: 8
    color: "#10121a"

    Repeater {
        model: root.controller.panes.paneLayout

        PaneLeaf {
            required property var modelData
            x: Math.round(Number(modelData.x) * root.width) + root.paneGap / 2
            y: Math.round(Number(modelData.y) * root.height) + root.paneGap / 2
            width: Math.max(1, Math.round(Number(modelData.width) * root.width) - root.paneGap)
            height: Math.max(1, Math.round(Number(modelData.height) * root.height) - root.paneGap)
            controller: root.controller
            node: modelData
            onOpenConnectionRequested: root.openConnectionRequested()
            onQueueRequested: session => root.queueRequested(session)
            onProfileRequested: session => root.profileRequested(session)
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
            width: vertical ? root.paneGap : splitWidth
            height: vertical ? splitHeight : root.paneGap
            color: dragHandler.active ? "#9ca3d1" : hoverHandler.hovered ? "#555b7c" : "#10121a"
            z: 10

            HoverHandler {
                id: hoverHandler
            }
            DragHandler {
                id: dragHandler
                xAxis.enabled: splitHandle.vertical
                yAxis.enabled: !splitHandle.vertical
                xAxis.minimum: splitHandle.splitX + splitHandle.splitWidth * 0.15 - splitHandle.width / 2
                xAxis.maximum: splitHandle.splitX + splitHandle.splitWidth * 0.85 - splitHandle.width / 2
                yAxis.minimum: splitHandle.splitY + splitHandle.splitHeight * 0.15 - splitHandle.height / 2
                yAxis.maximum: splitHandle.splitY + splitHandle.splitHeight * 0.85 - splitHandle.height / 2
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
