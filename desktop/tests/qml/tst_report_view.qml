import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "ReportView"
    when: windowShown
    width: 700
    height: 520
    visible: true

    property var opened: []

    QtObject {
        id: stubController
        property var updateArtifacts: []
        function openExternalLink(link) { testCase.opened.push(link); return true; }
        function artifactIsViewableReport(a) { return true; }
        function reportForArtifact(id) {
            if (id === "html-report")
                return {artifact_id: id, title: "Deployment review",
                        summary: "What changed", type: "document", isHtml: true,
                        body: '<h1>Deployment review</h1>'
                            + '<p>Latency returned to baseline.</p>'
                            + '<table border="1"><thead><tr><th>Service</th><th>p95</th></tr></thead>'
                            + '<tbody><tr><td>api</td><td>240 ms</td></tr></tbody></table>'
                            + '<ul><li>first</li><li>second</li></ul>'
                            + '<p><a href="https://example.com/run">the run log</a></p>'};
            if (id === "markdown-report")
                return {artifact_id: id, title: "Findings", summary: "", type: "research",
                        isHtml: false, body: "# Findings\n\nOne thing.\n\n- a\n- b"};
            return ({});
        }
    }

    Component {
        id: viewer
        Clarp.ReportView {
            width: 700
            height: 520
            controller: stubController
        }
    }

    function test_htmlReportRendersWithLayoutAndLinks() {
        const view = createTemporaryObject(viewer, testCase);
        verify(view !== null);
        view.open("html-report");
        waitForRendering(view);

        const body = findChild(view, "reportBody");
        verify(body !== null, "the report body editor must exist");

        // The regression this guards: a zero-width editor inside the ScrollView
        // collapsed the whole report to an invisible strip.
        verify(body.width > 200, "report body must take the view width, got " + body.width);
        verify(body.implicitHeight > 60,
               "rendered report must have real height, got " + body.implicitHeight);

        // Structure survives Qt's rich text reader.
        const plain = body.getText(0, body.length);
        verify(plain.indexOf("Deployment review") >= 0, "heading missing: " + plain);
        verify(plain.indexOf("240 ms") >= 0, "table cell missing: " + plain);
        verify(plain.indexOf("second") >= 0, "list item missing: " + plain);

        compare(findChild(view, "reportKind").text, "HTML");
        compare(findChild(view, "reportTitle").text, "Deployment review");
    }

    function test_reportLinksOpenThroughTheController() {
        testCase.opened = [];
        const view = createTemporaryObject(viewer, testCase);
        view.open("html-report");
        waitForRendering(view);
        const body = findChild(view, "reportBody");

        let spot = null;
        for (var y = 2; y < body.height && spot === null; y += 4) {
            for (var x = 0; x < body.width; x += 3) {
                if (body.linkAt(x, y).length > 0) { spot = {x: x + 2, y: y}; break; }
            }
        }
        verify(spot !== null, "the report must contain a clickable link");

        const hover = findChild(body, "reportLinkHover");
        verify(hover !== null);
        mouseMove(body, spot.x, spot.y);
        tryVerify(function() { return body.hoveredLink.length > 0; }, 2000);
        compare(hover.cursorShape, Qt.PointingHandCursor);

        mouseClick(body, spot.x, spot.y);
        tryVerify(function() { return testCase.opened.length === 1; }, 2000);
        compare(testCase.opened[0], "https://example.com/run");
    }

    function test_markdownReportUsesTheMarkdownReader() {
        const view = createTemporaryObject(viewer, testCase);
        view.open("markdown-report");
        waitForRendering(view);
        const body = findChild(view, "reportBody");
        compare(findChild(view, "reportKind").text, "MARKDOWN");
        const plain = body.getText(0, body.length);
        // Rendered, not shown as raw markdown source.
        verify(plain.indexOf("#") < 0, "markdown must be rendered, got: " + plain);
        verify(plain.indexOf("Findings") >= 0);
    }

    function test_escapeClosesTheViewer() {
        const view = createTemporaryObject(viewer, testCase);
        view.open("html-report");
        waitForRendering(view);
        let closed = 0;
        view.closeRequested.connect(function() { closed++; });
        keyClick(Qt.Key_Escape);
        tryVerify(function() { return closed === 1; }, 2000, "Escape must request close");
    }
}
