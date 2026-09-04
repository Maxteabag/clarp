import QtQuick

Rectangle {
    id: root

    required property string status
    property string label: status.length > 0 ? status : "offline"
    property color tone: {
        if (status === "thinking" || status === "tool" || status === "compacting")
            return "#e5a769";
        if (status === "waiting" || status === "interrupted")
            return "#e27d72";
        if (status === "live" || status === "done" || status === "idle")
            return "#77c7a3";
        return "#77717f";
    }

    implicitWidth: labelText.implicitWidth + 20
    implicitHeight: 24
    radius: 12
    color: Qt.rgba(tone.r, tone.g, tone.b, 0.12)
    border.color: Qt.rgba(tone.r, tone.g, tone.b, 0.34)
    border.width: 1

    Row {
        anchors.centerIn: parent
        spacing: 6

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: 6
            height: 6
            radius: 3
            color: root.tone
        }

        Text {
            id: labelText
            anchors.verticalCenter: parent.verticalCenter
            text: root.label.toUpperCase()
            color: root.tone
            font.pixelSize: 10
            font.weight: Font.DemiBold
            font.letterSpacing: 0.7
        }
    }
}
