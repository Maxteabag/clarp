import QtQuick
import QtQuick.Controls

// Background work for one agent: the blue hourglass while it waits on
// background jobs, the animated agent glyph while a sub-agent or helper runs,
// and a small count once more than one process is running. Clicking asks the
// owner to open the process list.
Item {
    id: root
    objectName: "processIndicator"

    property int jobCount: 0
    property int subAgentCount: 0
    property int runningChildren: 0
    property bool reducedMotion: false
    property real glyphSize: 14
    readonly property int total: Math.max(0, jobCount - subAgentCount) + Math.max(subAgentCount, runningChildren, 0)
    readonly property bool subAgentMode: subAgentCount > 0 || runningChildren > 0
    readonly property string glyphKind: subAgentMode ? "agent" : "hourglass"
    readonly property string summary: {
        const parts = [];
        const plain = Math.max(0, jobCount - subAgentCount);
        const agents = Math.max(0, subAgentCount, runningChildren);
        if (plain > 0) parts.push(plain + (plain === 1 ? " background job" : " background jobs"));
        if (agents > 0) parts.push(agents + (agents === 1 ? " sub-agent" : " sub-agents"));
        return parts.join(", ");
    }
    signal clicked

    visible: total > 0
    implicitWidth: visible ? glyphSize + (badge.visible ? badge.width * 0.8 : 0) + 2 : 0
    implicitHeight: glyphSize + 2
    Accessible.role: Accessible.Button
    Accessible.name: summary.length > 0 ? summary + " running" : "Nothing running"

    ProcessGlyph {
        id: glyph
        objectName: "processIndicatorGlyph"
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        glyphSize: root.glyphSize
        kind: root.glyphKind
        running: root.subAgentMode
        reducedMotion: root.reducedMotion
    }

    Rectangle {
        id: badge
        objectName: "processIndicatorBadge"
        visible: root.total > 1
        anchors.left: glyph.left
        anchors.leftMargin: root.glyphSize * 0.8
        anchors.top: parent.top
        anchors.topMargin: -3
        width: Math.max(11, badgeLabel.implicitWidth + 5)
        height: 11
        radius: 5.5
        color: Theme.link
        border.color: Theme.window
        border.width: 1

        Text {
            id: badgeLabel
            anchors.centerIn: parent
            text: root.total > 9 ? "9+" : String(root.total)
            color: Theme.window
            font.family: Theme.fontFamily
            font.pixelSize: 8
            font.weight: Font.Bold
        }
    }

    MouseArea {
        anchors.fill: parent
        anchors.margins: -4
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
        ToolTip.visible: containsMouse
        ToolTip.delay: 400
        ToolTip.text: root.summary + " running"
    }
}
