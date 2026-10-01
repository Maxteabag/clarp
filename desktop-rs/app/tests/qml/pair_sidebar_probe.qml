// needs: fake-host
// C++ PairSidebarSmokeCheck (CLARP_SCREENSHOT_PAIR_SIDEBAR) on the real
// Main: agent-to-agent rooms are a secondary list behind one row, so the
// agent list and the room list swap rather than stack, and the archive
// replaces the room list.
import QtQuick
import Clarp.Desktop

Main {
    id: root
    width: 1100; height: 700
    sidebarVisible: true
    property int failures: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    function find(name) {
        const seen = new Set()
        const walk = (o) => {
            if (!o || seen.has(o)) return null
            seen.add(o)
            if (o.objectName === name) return o
            for (const list of [o.data, o.children, o.resources]) {
                if (!list) continue
                for (let i = 0; i < list.length; ++i) { const f = walk(list[i]); if (f) return f }
            }
            return null
        }
        return walk(root.contentItem) || walk(root)
    }
    property int step: 0
    Timer {
        interval: 150; repeat: true; running: true
        onTriggered: {
            const list = find("chatList"), agents = find("sidebarAgentList"), rooms = find("pairConversationList")
            if (!list || !agents || !rooms) { if (step++ > 30) { check(false, "sidebar objects"); stop(); finish() } return }
            if (step < 1000) step = 1000
            switch (step++) {
            case 1000:
                check(!list.showingPairs, "rooms start collapsed")
                check(agents.visible && !rooms.visible, "the agent list owns the sidebar")
                list.showingPairs = true
                break
            case 1001:
                check(rooms.visible && !agents.visible, "opening rooms swaps the lists, never stacks them")
                list.showingArchive = true
                break
            case 1002:
                check(!rooms.visible, "the archive replaces the room list")
                list.showingArchive = false
                list.showingPairs = false
                break
            default:
                check(agents.visible && !rooms.visible, "closing brings the agent list back")
                stop()
                finish()
            }
        }
    }
}
