import QtQuick

// The two background-work marks, drawn so they follow the reading theme:
// "hourglass" is waiting on a background job, "agent" is a running sub-agent.
// Only the agent glyph moves, and only while `running` and motion is allowed.
Item {
    id: root

    property string kind: "hourglass"
    property color color: Theme.link
    property bool running: false
    property bool reducedMotion: false
    property real glyphSize: 14
    readonly property bool animating: root.kind === "agent" && root.running && !root.reducedMotion
        && root.visible

    implicitWidth: glyphSize
    implicitHeight: glyphSize

    Canvas {
        id: canvas
        objectName: "processGlyphCanvas"
        width: root.glyphSize
        height: root.glyphSize
        anchors.horizontalCenter: parent.horizontalCenter
        y: 0
        renderStrategy: Canvas.Cooperative
        onPaint: {
            const ctx = getContext("2d");
            const s = width;
            ctx.reset();
            ctx.strokeStyle = root.color;
            ctx.fillStyle = root.color;
            ctx.lineWidth = Math.max(1, s / 10);
            ctx.lineCap = "round";
            ctx.lineJoin = "round";
            if (root.kind === "agent") {
                // Antenna, a rounded head with two eyes, and shoulders.
                ctx.beginPath();
                ctx.moveTo(s * 0.5, s * 0.08);
                ctx.lineTo(s * 0.5, s * 0.2);
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(s * 0.5, s * 0.08, s * 0.06, 0, Math.PI * 2);
                ctx.fill();
                const x = s * 0.22, y = s * 0.2, w = s * 0.56, h = s * 0.42, r = s * 0.1;
                ctx.beginPath();
                ctx.moveTo(x + r, y);
                ctx.lineTo(x + w - r, y);
                ctx.arcTo(x + w, y, x + w, y + r, r);
                ctx.lineTo(x + w, y + h - r);
                ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
                ctx.lineTo(x + r, y + h);
                ctx.arcTo(x, y + h, x, y + h - r, r);
                ctx.lineTo(x, y + r);
                ctx.arcTo(x, y, x + r, y, r);
                ctx.closePath();
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(s * 0.39, s * 0.41, s * 0.06, 0, Math.PI * 2);
                ctx.arc(s * 0.61, s * 0.41, s * 0.06, 0, Math.PI * 2);
                ctx.fill();
                ctx.beginPath();
                ctx.moveTo(s * 0.14, s * 0.95);
                ctx.quadraticCurveTo(s * 0.5, s * 0.62, s * 0.86, s * 0.95);
                ctx.stroke();
            } else {
                // Two bars and the glass, with sand settled in the lower bulb.
                ctx.beginPath();
                ctx.moveTo(s * 0.2, s * 0.08);
                ctx.lineTo(s * 0.8, s * 0.08);
                ctx.moveTo(s * 0.2, s * 0.92);
                ctx.lineTo(s * 0.8, s * 0.92);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(s * 0.28, s * 0.1);
                ctx.lineTo(s * 0.28, s * 0.24);
                ctx.lineTo(s * 0.5, s * 0.5);
                ctx.lineTo(s * 0.28, s * 0.76);
                ctx.lineTo(s * 0.28, s * 0.9);
                ctx.moveTo(s * 0.72, s * 0.1);
                ctx.lineTo(s * 0.72, s * 0.24);
                ctx.lineTo(s * 0.5, s * 0.5);
                ctx.lineTo(s * 0.72, s * 0.76);
                ctx.lineTo(s * 0.72, s * 0.9);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(s * 0.34, s * 0.88);
                ctx.lineTo(s * 0.5, s * 0.68);
                ctx.lineTo(s * 0.66, s * 0.88);
                ctx.closePath();
                ctx.fill();
            }
        }

        SequentialAnimation on y {
            running: root.animating
            loops: Animation.Infinite
            NumberAnimation { to: -root.glyphSize * 0.1; duration: 420; easing.type: Easing.OutQuad }
            NumberAnimation { to: 0; duration: 420; easing.type: Easing.InQuad }
            PauseAnimation { duration: 360 }
        }
    }

    onAnimatingChanged: if (!animating) canvas.y = 0
    onKindChanged: canvas.requestPaint()
    onColorChanged: canvas.requestPaint()
    onGlyphSizeChanged: canvas.requestPaint()
}
