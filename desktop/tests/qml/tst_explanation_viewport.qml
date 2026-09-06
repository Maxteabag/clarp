import QtQuick
import QtTest
import "../../qml/components"

TestCase {
    id: testCase
    name: "ExplanationViewport"
    when: windowShown
    width: 400; height: 300
    visible: true
    QtObject {
        id: fakeNarrator
        property bool enabled: true
        property int revision: 0
        property bool unavailable: false
        property int acquisitions: 0
        property int releases: 0
        signal changed()
        function explanation() { return ""; }
        function acquireView(owner, activity) { acquisitions++; }
        function releaseView(owner) { releases++; }
    }
    Component {
        id: fixture
        Flickable {
            objectName: "transcriptList"
            width: 300; height: 100; contentHeight: 1000; clip: true
            Item {
                y: 400; width: 300; height: 50
                ActivityExplanation { narrator: fakeNarrator; activity: ({command: "ls"}) }
            }
        }
    }
    function test_onlyIntersectingRowsAcquireAfterDwell() {
        fakeNarrator.acquisitions=0; fakeNarrator.releases=0;
        const view=createTemporaryObject(fixture,testCase);
        wait(300);
        compare(fakeNarrator.acquisitions,0);
        view.contentY=380;
        tryCompare(fakeNarrator,"acquisitions",1,1000);
        view.contentY=0;
        tryCompare(fakeNarrator,"releases",1,1000);
        view.contentY=380;
        wait(90);
        view.contentY=0;
        wait(350);
        compare(fakeNarrator.acquisitions,1);
    }
}
