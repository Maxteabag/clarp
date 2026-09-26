pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// What an agent is running right now: one line per active background job
// (kind, title, how long it has run, last heartbeat) and one per running child
// helper. Choosing a helper opens its conversation. Escape and a click outside
// close it.
Popup {
    id: root
    objectName: "processPopover"

    required property var controller
    property string session: ""
    property int tick: 0
    readonly property var processes: {
        root.tick;
        root.controller.processRevision;
        root.controller.agentRevision;
        return root.session.length > 0 ? root.controller.agentProcesses(root.session) : ({});
    }
    readonly property var jobs: (root.processes && root.processes.jobs) || []
    readonly property var helpers: (root.processes && root.processes.helpers) || []
    // The snapshot may know about work the job list has not delivered yet.
    readonly property int unlisted: Math.max(0, Number(root.processes.jobCount || 0) - root.jobs.length)
        + Math.max(0, Number(root.processes.runningChildren || 0) - root.helpers.length)
    signal helperOpened(string session)

    function openFor(session, anchor) {
        root.session = session;
        if (anchor) {
            // Right-aligned under the anchor; `margins` keeps it inside the window.
            root.parent = anchor;
            root.x = anchor.width - root.width;
            root.y = anchor.height + 4;
        }
        root.open();
    }

    width: 340
    padding: 0
    margins: 8
    focus: true
    modal: false
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onOpened: root.forceActiveFocus()

    Timer {
        interval: 1000
        repeat: true
        running: root.visible
        onTriggered: root.tick++
    }

    background: Rectangle {
        color: Theme.raised
        border.color: Theme.border
        border.width: 1
        radius: Theme.radius
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 10
            Layout.bottomMargin: 6
            spacing: 8
            TuiText {
                Layout.fillWidth: true
                text: root.controller.agentName(root.session)
                color: Theme.text
                font.pixelSize: 12
                font.weight: Font.DemiBold
                elide: Text.ElideRight
            }
            TuiText {
                objectName: "processPopoverCount"
                text: {
                    const n = Number(root.processes.total || 0);
                    return n === 0 ? "nothing running" : n + " running";
                }
                color: Theme.muted
                font.pixelSize: 11
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.rule }

        Repeater {
            model: root.jobs

            RowLayout {
                id: jobRow
                required property var modelData
                objectName: "processJobRow"
                Layout.fillWidth: true
                Layout.leftMargin: 10
                Layout.rightMargin: 10
                Layout.topMargin: 6
                Layout.bottomMargin: 6
                spacing: 8

                ProcessGlyph {
                    Layout.alignment: Qt.AlignTop
                    Layout.topMargin: 1
                    glyphSize: 13
                    kind: jobRow.modelData.subAgent ? "agent" : "hourglass"
                    running: Boolean(jobRow.modelData.subAgent)
                    reducedMotion: Boolean(root.controller.avatarMotion && root.controller.avatarMotion.reducedMotion)
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    TuiText {
                        Layout.fillWidth: true
                        textFormat: Text.PlainText
                        text: String(jobRow.modelData.title || "")
                        color: Theme.text
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }
                    TuiText {
                        Layout.fillWidth: true
                        textFormat: Text.PlainText
                        visible: text.length > 0
                        text: {
                            const kind = jobRow.modelData.subAgent ? "sub-agent" : String(jobRow.modelData.kind || "job");
                            const detail = String(jobRow.modelData.detail || "");
                            const status = String(jobRow.modelData.status || "");
                            return [kind, status === "queued" ? "queued" : "", detail].filter(p => p.length > 0).join(" · ");
                        }
                        color: Theme.muted
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                }
                ColumnLayout {
                    Layout.alignment: Qt.AlignTop
                    spacing: 1
                    TuiText {
                        Layout.alignment: Qt.AlignRight
                        text: String(jobRow.modelData.elapsed || "")
                        color: Theme.secondary
                        font.pixelSize: 11
                    }
                    TuiText {
                        Layout.alignment: Qt.AlignRight
                        visible: text.length > 0
                        text: String(jobRow.modelData.heartbeat || "").length > 0
                            ? "♥ " + jobRow.modelData.heartbeat : ""
                        color: Theme.faint
                        font.pixelSize: 10
                    }
                }
            }
        }

        Repeater {
            model: root.helpers

            ItemDelegate {
                id: helperRow
                required property var modelData
                objectName: "processHelperRow"
                Layout.fillWidth: true
                leftPadding: 10
                rightPadding: 10
                topPadding: 6
                bottomPadding: 6
                hoverEnabled: true
                Accessible.name: "Open " + String(helperRow.modelData.name || "helper")
                onClicked: {
                    const session = String(helperRow.modelData.session || "");
                    root.close();
                    root.helperOpened(session);
                    root.controller.selectSession(session);
                }
                background: Rectangle { color: helperRow.hovered ? Theme.control : "transparent" }
                contentItem: RowLayout {
                    spacing: 8
                    ProcessGlyph {
                        glyphSize: 13
                        kind: "agent"
                        running: true
                        reducedMotion: Boolean(root.controller.avatarMotion && root.controller.avatarMotion.reducedMotion)
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        TuiText {
                            Layout.fillWidth: true
                            textFormat: Text.PlainText
                            text: String(helperRow.modelData.name || "")
                            color: Theme.text
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }
                        TuiText {
                            Layout.fillWidth: true
                            textFormat: Text.PlainText
                            text: "helper" + (String(helperRow.modelData.statusText || "").length > 0
                                ? " · " + helperRow.modelData.statusText : "")
                            color: Theme.muted
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                    }
                    TuiText {
                        text: "›"
                        color: Theme.secondary
                        font.pixelSize: 14
                    }
                }
            }
        }

        TuiText {
            Layout.fillWidth: true
            Layout.margins: 10
            visible: root.unlisted > 0 || (root.jobs.length === 0 && root.helpers.length === 0)
            text: root.jobs.length === 0 && root.helpers.length === 0 && root.unlisted === 0
                ? "Nothing running."
                : root.unlisted + " more reported by the Host, details not loaded yet."
            color: Theme.muted
            font.pixelSize: 11
            wrapMode: Text.Wrap
        }
        Item { Layout.preferredHeight: 4 }
    }
}
