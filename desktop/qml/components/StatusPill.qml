import QtQuick

Rectangle {
    id: root

    required property string status
    property string label: status.length > 0 ? status : "offline"
    readonly property bool quiet: status === "live" || status === "done" || status === "idle"
    property color tone: {
        if (status === "thinking" || status === "tool" || status === "compacting")
            return "#91a67f";
        if (status === "waiting" || status === "interrupted")
            return "#c47d8d";
        if (status === "live" || status === "done" || status === "idle")
            return "#7e9573";
        return "#6b6e84";
    }

    visible: !quiet
    implicitWidth: visible ? labelText.implicitWidth + 15 : 0
    implicitHeight: visible ? 18 : 0
    color: "transparent"

    Row {
        anchors.centerIn: parent
        spacing: 5

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: 5
            height: 5
            radius: 2.5
            color: root.tone
        }

        Text {
            id: labelText
            anchors.verticalCenter: parent.verticalCenter
            text: root.label.toUpperCase()
            color: root.tone
            font.family: "JetBrains Mono"
            font.pixelSize: 8
            font.weight: Font.DemiBold
            font.letterSpacing: 0.5
        }
    }
}
