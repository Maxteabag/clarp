pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

Rectangle {
    id: root

    required property var controller
    signal openConnectionRequested
    signal queueRequested(string session)
    signal profileRequested(string session)
    readonly property real paneGap: 4
    readonly property real toolbarHeight: controller.panes.workspaceSaveWarning ? 76 : 36
    property int createdViews: 0
    function syncViews() {
        const next=controller.panes.viewLayout;
        for(let i=placements.count-1;i>=0;--i)
            if(!next.some(p=>p.id===placements.get(i).paneKey)) placements.remove(i);
        for(const p of next) {
            let found=-1;for(let i=0;i<placements.count;++i)if(placements.get(i).paneKey===p.id)found=i;
            const value={paneKey:p.id,paneSession:p.session||"",px:p.x,py:p.y,pw:p.width,ph:p.height,shown:!!p.shown};
            if(found<0) placements.append(value);else placements.set(found,value);
        }
    }
    ListModel {id:placements}
    Connections {target:root.controller.panes; function onTreeChanged(){root.syncViews();}}
    Component.onCompleted: syncViews()
    ScrollView {
        width: root.width
        height: 36
        z: 50
        clip: true
        contentWidth: workspaceBar.implicitWidth
        contentHeight: 36
        ScrollBar.vertical.policy: ScrollBar.AlwaysOff
        Row {
        id:workspaceBar; height:36; spacing:4; z:50
        Repeater {model:root.controller.panes.workspaces
            TuiButton {id:workspaceTab;required property var modelData; text:modelData.name; checkable:true
                contentItem:TuiLabel {text:workspaceTab.text;color:"#e7e1dc";horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter}
                checked:root.controller.panes.activeWorkspace===modelData.id
                onClicked:root.controller.panes.switchWorkspace(modelData.id)
            }
        }
        TuiButton {text:"+ Workspace"; enabled:root.controller.panes.workspaces.length<8; onClicked:root.controller.panes.createWorkspace("Workspace "+(root.controller.panes.workspaces.length+1))}
        TuiButton {text:"Move to workspace"; onClicked:moveMenu.open()
            Menu {id:moveMenu
                Repeater {model:root.controller.panes.workspaces
                    MenuItem {required property var modelData;text:modelData.name;enabled:modelData.id!==root.controller.panes.activeWorkspace
                        onTriggered:root.controller.panes.moveActiveToWorkspace(modelData.id)}
                }
            }
        }
    }
    }
    Row {
        y: 36; height: 40; width: root.width; spacing: 8; z: 50
        visible: !!root.controller.panes.workspaceSaveWarning
        TuiLabel {width:Math.max(100,root.width-saveLayoutButton.width-8);text:root.controller.panes.workspaceSaveWarning;wrapMode:Text.Wrap;color:"#ffcb8c"}
        TuiButton {id:saveLayoutButton;text:"Save this layout instead";onClicked:root.controller.panes.saveWorkspaceLayoutInstead()}
    }
    color: "#10121a"

    function focusNavigation() { navigationFocus.forceActiveFocus(); }
    Item { id: navigationFocus; objectName: "conversationNavigationFocus" }

    function jumpToLatest() {
        for (let i = 0; i < paneViews.count; ++i) {
            const pane = paneViews.itemAt(i) as PaneLeaf;
            if (pane && pane.active) { pane.jumpToLatest(); return; }
        }
    }

    Repeater {
        id: paneViews
        model: placements

        PaneLeaf {
            required property string paneKey
            required property string paneSession
            required property real px
            required property real py
            required property real pw
            required property real ph
            required property bool shown
            visible: shown
            x: Math.round(px * root.width) + root.paneGap / 2
            y: root.toolbarHeight + Math.round(py * (root.height-root.toolbarHeight)) + root.paneGap / 2
            width: Math.max(1, Math.round(pw * root.width) - root.paneGap)
            height: Math.max(1, Math.round(ph * (root.height-root.toolbarHeight)) - root.paneGap)
            controller: root.controller
            node: ({id:paneKey,session:paneSession})
            Component.onCompleted: root.createdViews++
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
            readonly property real splitY: root.toolbarHeight + Number(modelData.y) * (root.height-root.toolbarHeight)
            readonly property real splitWidth: Number(modelData.width) * root.width
            readonly property real splitHeight: Number(modelData.height) * (root.height-root.toolbarHeight)

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
