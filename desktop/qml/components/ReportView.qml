pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A read-only viewer for agent-published reports, rendered by Qt's own rich
// text engine. Nothing here embeds a browser, so the native-only gate holds.
Rectangle {
    id: root
    objectName: "reportView"

    required property var controller
    property string artifactId: ""
    readonly property var report: {
        controller.updateArtifacts;
        return root.artifactId.length > 0 ? controller.reportForArtifact(root.artifactId) : ({});
    }
    readonly property bool hasReport: Boolean(root.report && root.report.body)
    signal closeRequested

    // A reading surface, not a translucent scrim: report text must stay legible
    // and must not compete with the conversation behind it.
    color: "#0b0c12"
    visible: false
    focus: visible

    onVisibleChanged: if (visible) reportBody.forceActiveFocus()

    Keys.onEscapePressed: event => { root.closeRequested(); event.accepted = true; }

    function open(artifactId) {
        root.artifactId = artifactId;
        root.visible = true;
    }

    // The body is already sanitized in C++ when it is HTML; Qt's Markdown
    // reader cannot reference remote resources at all.
    function applyReport() {
        reportBody.textFormat = root.report.isHtml
            ? TextEdit.RichText : TextEdit.MarkdownText;
        // Clear first so a re-open always re-parses with the current reader.
        reportBody.text = "";
        reportBody.text = String(root.report.body || "");
    }
    onReportChanged: root.applyReport()
    Component.onCompleted: root.applyReport()

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                TuiText {
                    objectName: "reportTitle"
                    Layout.fillWidth: true
                    text: String(root.report.title || "Report")
                    color: "#e7e1dc"
                    font.pixelSize: 17
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                TuiText {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: String(root.report.summary || "")
                    color: "#868a9f"
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
            }
            TuiText {
                objectName: "reportKind"
                text: (root.report.isHtml ? "HTML" : "MARKDOWN")
                color: "#72778f"
                font.family: "JetBrains Mono"
                font.pixelSize: 11
            }
            TuiButton {
                objectName: "reportClose"
                text: "✕"
                implicitHeight: 27
                implicitWidth: 32
                onClicked: root.closeRequested()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 1
            color: "#2a2c3b"
        }

        ScrollView {
            id: reportScroll
            objectName: "reportScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth

            TextEdit {
                id: reportBody
                objectName: "reportBody"
                // Inside a ScrollView the parent is the Flickable content item,
                // whose width starts at 0; a zero width collapses the rich text
                // to nothing. Bind to the view's available width instead.
                width: reportScroll.availableWidth
                readOnly: true
                selectByMouse: true
                persistentSelection: true
                // text and textFormat must not be independent bindings. If the
                // body arrives while the format is still the default, Qt parses
                // it with the wrong reader, replaces `text` with the serialized
                // result, and the later format change re-parses that already
                // emptied document. Assign the format first, then the body.
                textFormat: TextEdit.RichText
                wrapMode: Text.Wrap
                color: "#e7e1dc"
                selectedTextColor: "#fff8ff"
                selectionColor: "#6f527b"
                font.pixelSize: 15
                onLinkActivated: link => root.controller.openExternalLink(link)

                HoverHandler {
                    objectName: "reportLinkHover"
                    cursorShape: reportBody.hoveredLink.length > 0
                        ? Qt.PointingHandCursor : Qt.IBeamCursor
                }
            }
        }

        TuiText {
            objectName: "reportEmpty"
            visible: !root.hasReport
            Layout.fillWidth: true
            text: "This artifact has no readable body."
            color: "#868a9f"
            font.pixelSize: 13
        }
    }
}
