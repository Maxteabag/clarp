import QtQuick

Rectangle {
    id: root

    required property string status
    property bool showIndicator: true
    property string label: status.length > 0 ? status : "offline"
    readonly property bool quiet: status === "live" || status === "done" || status === "idle"
    property color tone: {
        if (status === "thinking" || status === "tool" || status === "compacting")
            return Theme.success;
        if (status === "waiting" || status === "interrupted")
            return Theme.danger;
        if (status === "live" || status === "done" || status === "idle")
            return Theme.success;
        return Theme.faint;
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
            visible: root.showIndicator
            width: visible ? 5 : 0
            height: 5
            radius: 0
            color: root.tone
        }

        TuiText {
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
