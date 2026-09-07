import QtQuick
import QtTest
import "../../qml/components"

TestCase {
    name: "ComposerGrowth"
    width: 600
    height: 500
    when: windowShown

    QtObject {
        id: controller
        property int agentRevision: 0
        property bool muted: false
        property int composerRevision: 0
        property string startingContact: ""
        property string composerFocusPane: ""
        signal draftChanged(string session, string text, string originPaneId)
        function agentQueueCount(session) { return 0; }
        function composerAttachments(pane, session) { return []; }
        function composerCanSend(pane, session) { return true; }
        function paneDraft(pane, session) { return ""; }
        function setPaneDraft(pane, session, text) {}
        function requestComposerFocus(pane) {}

        property var audio: ({playing: false, paused: false, recording: false, transcribing: false, transcriptionsInFlight: 0, transcriptionsForSession: function(session) { return 0; }})
        property string sent: ""
        function agentName(session) { return session; }
        function agentState(session) { return "idle"; }
        function sendComposerMessage(pane, session, text, queued) { sent = text; return true; }
    }

    Composer {
        id: composer
        width: 600
        height: implicitHeight
        controller: controller
        session: "fixture"
        paneId: "pane"
        active: true
    }

    function test_growthAndSend() {
        const editor = findChild(composer, "paneComposerEditor");
        verify(editor !== null);
        compare(composer.height, 54);
        editor.text = "First\nSecond\nThird\nFourth";
        tryVerify(() => composer.height > 54);
        editor.text = "wrapped words ".repeat(40);
        tryVerify(() => composer.height > 54);
        editor.text = "line\n".repeat(100);
        tryCompare(composer, "height", 214);
        editor.clear();
        tryCompare(composer, "height", 54);
        editor.forceActiveFocus();
        editor.text = "hello";
        editor.cursorPosition = editor.length;
        keyClick(Qt.Key_Return, Qt.ShiftModifier);
        verify(editor.text.indexOf("\n") >= 0);
        keyClick(Qt.Key_Return);
        compare(controller.sent, "hello\n");
        compare(editor.text, "");
        tryCompare(composer, "height", 54);
    }
}
