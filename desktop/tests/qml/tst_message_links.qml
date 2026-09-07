import QtQuick
import QtTest
import "../../qml/components" as Clarp

TestCase {
    id: testCase
    name: "MessageLinks"
    when: windowShown
    width: 620
    height: 480
    visible: true

    QtObject {
        id: stubController
        property int mediaRevision: 0
        property var toolNarrator: null
        property int avatarRevision: 0
        property int agentRevision: 0
        property bool sharedFilesystem: false
        // Records what the delegate asked the controller to do.
        property var opened: []
        property var copied: []
        function avatarSource(session) { return ""; }
        function agentSessionById(id) { return ""; }
        function agentWorkingDirectory(session) { return ""; }
        function resolveMediaMarkdown(text) { return text; }
        function markdownDisplayBlocks(text) { return text.split("\n\n"); }
        function openExternalLink(link) { opened.push(link); return true; }
        function copyToClipboard(text) { copied.push(text); }
        function canLinkifyOutput(text) { return text.indexOf("http") >= 0; }
        function linkifiedOutput(text) {
            return '<div style="white-space: pre-wrap;">'
                + text.replace("https://example.com/pr/7",
                    '<a href="https://example.com/pr/7">https://example.com/pr/7</a>')
                + '</div>';
        }
    }

    Component {
        id: linkMessage
        Clarp.MessageDelegate {
            width: 600
            controller: stubController
            session: "fixture"
            messageId: "links"
            authorRole: "assistant"
            body: "read https://example.com/releases now"
            timestamp: ""
            messageKind: ""
            toolName: ""
            origin: ""
            senderName: ""
            pending: false
            deliveryFailed: false
            activity: false
            activityStatus: ""
            automated: false
            category: ""
            tools: []
            displayCells: []
            activityCount: 0
            toolDetailsAvailable: false
            showTools: false
            showTimestamp: false
        }
    }

    Component {
        id: toolMessage
        Clarp.MessageDelegate {
            width: 600
            controller: stubController
            session: "fixture"
            messageId: "tool"
            authorRole: "assistant"
            body: ""
            timestamp: ""
            messageKind: ""
            toolName: ""
            origin: ""
            senderName: ""
            pending: false
            deliveryFailed: false
            activity: false
            activityStatus: ""
            automated: false
            category: ""
            tools: [{name: "Bash", summary: "git push",
                     command: "git push", result: "see https://example.com/pr/7"}]
            displayCells: []
            activityCount: 1
            toolDetailsAvailable: false
            showTools: true
            showTimestamp: false
        }
    }

    // Scan a text editor row by row for the first pixel that reports a link.
    function firstLinkPoint(editor) {
        for (var y = 2; y < Math.max(4, editor.height); y += 4) {
            for (var x = 0; x < editor.width; x += 2) {
                if (editor.linkAt(x, y).length > 0)
                    return {x: x + 3, y: y, link: editor.linkAt(x, y)};
            }
        }
        return null;
    }

    function test_bareUrlIsHoverableClickableAndCopyable() {
        stubController.opened = [];
        stubController.copied = [];
        const message = createTemporaryObject(linkMessage, testCase);
        verify(message !== null);
        waitForRendering(message);

        const editor = findChild(message, "messageTextBlock");
        verify(editor !== null, "message body editor must exist");

        // Qt's Markdown dialect autolinks a bare URL; prove it really is a link.
        const spot = firstLinkPoint(editor);
        verify(spot !== null, "a bare URL in the body must become a real link");
        compare(spot.link, "https://example.com/releases");

        // The pointing hand is the only cue that the blue text is actionable.
        const hover = findChild(editor, "messageLinkHover");
        verify(hover !== null, "the body needs a link hover handler");
        mouseMove(editor, spot.x, spot.y);
        tryVerify(function() { return editor.hoveredLink.length > 0; }, 2000,
                  "hovering a link must report it");
        compare(hover.cursorShape, Qt.PointingHandCursor);

        // Away from the link the caret must stay an I-beam for selection.
        mouseMove(editor, editor.width - 2, spot.y);
        tryVerify(function() { return editor.hoveredLink.length === 0; }, 2000);
        compare(hover.cursorShape, Qt.IBeamCursor);

        // Left click opens via the controller, never Qt.openUrlExternally.
        mouseMove(editor, spot.x, spot.y);
        mouseClick(editor, spot.x, spot.y);
        tryVerify(function() { return stubController.opened.length === 1; }, 2000,
                  "clicking a link must ask the controller to open it");
        compare(stubController.opened[0], "https://example.com/releases");

        // Right click offers copy without opening anything.
        mouseClick(editor, spot.x, spot.y, Qt.RightButton);
        const menu = findChild(editor, "messageLinkMenu");
        verify(menu !== null, "a link needs a context menu");
        tryVerify(function() { return menu.opened; }, 2000, "right click must open the menu");
        compare(menu.link, "https://example.com/releases");
        const copyItem = findChild(editor, "messageLinkCopy");
        verify(copyItem !== null);
        copyItem.triggered();
        compare(stubController.copied.length, 1);
        compare(stubController.copied[0], "https://example.com/releases");
        compare(stubController.opened.length, 1, "copying must not also open the link");
    }

    function test_dragStillSelectsTextRatherThanOpeningLinks() {
        stubController.opened = [];
        const message = createTemporaryObject(linkMessage, testCase);
        waitForRendering(message);
        const editor = findChild(message, "messageTextBlock");
        const spot = firstLinkPoint(editor);
        verify(spot !== null);

        // Selecting across a link must not count as activating it.
        mousePress(editor, 1, spot.y);
        mouseMove(editor, spot.x + 20, spot.y);
        mouseRelease(editor, spot.x + 20, spot.y);
        wait(80);
        verify(editor.selectedText.length > 0, "dragging must still select text");
        compare(stubController.opened.length, 0,
                "a selection drag must not open the link it crosses");
    }

    function test_toolOutputLinkOpensWithoutCollapsingTheCard() {
        stubController.opened = [];
        const message = createTemporaryObject(toolMessage, testCase);
        verify(message !== null);
        waitForRendering(message);

        const card = findChild(message, "toolCard");
        verify(card !== null, "the tool card must exist");
        card.expanded = true;
        waitForRendering(message);

        const editor = findChild(card, "toolLinkHover");
        verify(editor !== null, "expanded tool output needs a link hover handler");
        const detail = editor.parent;
        verify(detail.hoveredLink !== undefined);
        const spot = firstLinkPoint(detail);
        verify(spot !== null, "a URL in verbatim tool output must become a link");
        compare(spot.link, "https://example.com/pr/7");

        mouseMove(detail, spot.x, spot.y);
        tryVerify(function() { return detail.hoveredLink.length > 0; }, 2000);
        compare(editor.cursorShape, Qt.PointingHandCursor);

        mouseClick(detail, spot.x, spot.y);
        tryVerify(function() { return stubController.opened.length === 1; }, 2000,
                  "clicking a tool-output link must open it");
        compare(stubController.opened[0], "https://example.com/pr/7");
        // The card-wide tap toggles expansion, but the text editor consumes taps
        // that land inside it, so following a link must not also collapse it.
        compare(card.expanded, true, "opening a link must not collapse the tool card");
    }
}
