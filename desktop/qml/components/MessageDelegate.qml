import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    required property string messageId
    required property string authorRole
    required property string body
    required property string timestamp
    required property string messageKind
    required property string toolName
    required property string origin
    required property string senderName
    required property bool pending
    required property bool deliveryFailed
    required property bool activity
    required property var tools
    required property var displayCells
    required property bool showTools

    width: ListView.view ? ListView.view.width : 600
    visible: activity || body.length > 0 || (showTools && tools.length > 0)
    implicitHeight: visible ? content.implicitHeight + 12 : 0

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 5

        Item {
            Layout.fillWidth: true
            implicitHeight: root.activity ? 34 : messageBubble.visible ? messageBubble.implicitHeight : 0

            Rectangle {
                id: activityCard
                visible: root.activity
                x: 12
                width: parent.width - 24
                implicitHeight: 34
                radius: 10
                color: "#1d1a21"
                border.color: "#302b37"

                RowLayout {
                    id: activityRow
                    anchors.fill: parent
                    anchors.leftMargin: 11
                    anchors.rightMargin: 11
                    spacing: 8

                    BusyIndicator {
                        running: true
                        implicitWidth: 14
                        implicitHeight: 14
                    }
                    Text {
                        text: root.toolName || root.messageKind || "Working"
                        color: "#c49967"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.body
                        color: "#8b8491"
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: messageBubble
                visible: !root.activity && root.body.length > 0
                width: Math.min(parent.width * 0.82, Math.max(parent.width * 0.28, messageText.implicitWidth + 28))
                x: root.authorRole === "user" ? parent.width - width - 12 : 12
                implicitHeight: messageText.implicitHeight + 24
                radius: 14
                color: root.authorRole === "user" ? "#493651" : "#1b191f"
                border.width: 1
                border.color: root.deliveryFailed ? "#9b4f52" : root.authorRole === "user" ? "#65496f" : "#29252e"
                opacity: root.pending ? 0.68 : 1

                Text {
                    id: messageText
                    anchors.fill: parent
                    anchors.margins: 12
                    text: root.body
                    textFormat: Text.MarkdownText
                    wrapMode: Text.Wrap
                    color: "#e7e1dc"
                    linkColor: "#c293df"
                    font.pixelSize: 14
                    lineHeight: 1.22
                    lineHeightMode: Text.ProportionalHeight
                    onLinkActivated: link => Qt.openUrlExternally(link)
                }
            }
        }

        ColumnLayout {
            visible: root.showTools && root.tools.length > 0
            Layout.fillWidth: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            spacing: 5

            Repeater {
                model: root.tools

                ToolCard {
                    required property var modelData
                    Layout.fillWidth: true
                    tool: modelData
                }
            }
        }

        Text {
            visible: root.pending || root.deliveryFailed
            Layout.alignment: root.authorRole === "user" ? Qt.AlignRight : Qt.AlignLeft
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            text: root.deliveryFailed ? "Not delivered" : "Delivering…"
            color: root.deliveryFailed ? "#df7777" : "#77717f"
            font.pixelSize: 10
        }
    }
}
