// Offscreen check of the Rust contact and voice list models.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 400; visible: true
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    ContactListModel { id: contacts }
    VoiceListModel { id: voices }
    ListView {
        id: contactList; width: 200; height: 200; model: contacts
        delegate: Text { required property string name; required property string description; required property bool builtin; text: name }
    }
    ListView {
        id: voiceList; y: 200; width: 200; height: 200; model: voices
        delegate: Text { required property string voiceId; required property string label; required property string takenBy; required property bool current; text: label }
    }
    Component.onCompleted: {
        try {
            contacts.applySnapshotText(JSON.stringify({personas: [{id: "one", name: "Rachel"},
                {id: "two", name: "Bella", personality: "Personality: Thoughtful", builtin: true}]}), JSON.stringify(["RACHEL"]))
            contactList.forceLayout()
            const bella = contactList.itemAtIndex(0)
            check(contacts.count === 1 && bella && bella.name === "Bella", "active persona excluded (case-folded)")
            check(bella.description === "Thoughtful" && bella.builtin, "contact roles")
            voices.applyResponseText(JSON.stringify({voices: [{id: "v1", label: "Warm", taken_by: "Rachel"}, {id: "v2", taken_by: "Bella"}]}), "v1")
            voiceList.forceLayout()
            const first = voiceList.itemAtIndex(0), second = voiceList.itemAtIndex(1)
            check(voices.count === 2 && first.current && first.takenBy === "", "current voice hides its owner")
            check(second.label === "v2" && second.takenBy === "Bella", "label fallback and owner")
        } catch (e) { failures++; console.log("FAIL exception: " + e) }
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
}
