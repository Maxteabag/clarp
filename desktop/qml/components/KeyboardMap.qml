import QtQuick

// Child bindings override ancestors by action. A blocking state has no parent.
// Both Shortcut dispatch and footer hints consume the same resolved bindings.
QtObject {
    id: root
    property string contextName: "pane"
    property bool hasAgent: false
    property bool hasRows: false
    property bool canSend: false
    readonly property var parents: ({main: "root", workspace: "main", navigation: "workspace",
        pane: "navigation", sidebar: "navigation", composer: "workspace", search: "workspace",
        settings: "main", updates: "main", teams: "main", modal: "root", blocked: "root"})
    function binding(action, keys, label, hint = true, guard = "", native = false) {
        return {action: action, keys: keys, label: label, hint: hint, guard: guard, native: native};
    }
    readonly property var states: ({
        root: [],
        main: [binding("switcher", ["Ctrl+K"], "Commands"),
            binding("sidebar", ["Ctrl+B"], "Show/hide sidebar", false),
            binding("new", ["Ctrl+N"], "New agent", false),
            binding("new-contact", ["Ctrl+Alt+N"], "Start contact", false),
            binding("settings", ["Ctrl+,", "Ctrl+4"], "Settings", false),
            binding("chats", ["Ctrl+1"], "Chats", false), binding("updates", ["Ctrl+2"], "Updates", false),
            binding("teams", ["Ctrl+3"], "Teams", false), binding("refresh", ["Ctrl+R"], "Refresh", false),
            binding("mute", ["Ctrl+M"], "Mute", false), binding("overview", ["Ctrl+Shift+O"], "Overview", false),
            binding("tools", ["Ctrl+Shift+T"], "Tools", false),
            binding("ui-larger", ["Ctrl+="], "Larger", false), binding("ui-smaller", ["Ctrl+-"], "Smaller", false),
            binding("ui-reset", ["Ctrl+0"], "Reset scale", false)],
        workspace: [binding("escape", ["Escape"], "Navigate"),
            binding("move-left", ["Ctrl+Alt+Left"], "Left pane", false),
            binding("move-right", ["Ctrl+Alt+Right"], "Right pane", false),
            binding("move-up", ["Ctrl+Alt+Up"], "Upper pane", false),
            binding("move-down", ["Ctrl+Alt+Down"], "Lower pane", false),
            binding("split-right", ["Ctrl+Alt+V"], "Split right", false),
            binding("split-down", ["Ctrl+Alt+S"], "Split down", false),
            binding("close-pane", ["Ctrl+Alt+X"], "Close pane", false),
            binding("zoom", ["Ctrl+Alt+Z"], "Zoom pane", false),
            binding("balance", ["Ctrl+Alt+="], "Balance panes", false),
            binding("agent-terminal", ["Ctrl+Alt+T"], "Terminal", false, "agent"),
            binding("release-agent", ["Ctrl+Shift+R"], "Release", false, "agent"),
            binding("stop-agent", ["Ctrl+."], "Stop", false, "agent"),
            binding("talk", ["Ctrl+Shift+Space"], "Talk", false, "agent")],
        navigation: [binding("focus-sidebar", ["E"], "Agents"),
            binding("focus-pane", ["C"], "Conversation"),
            binding("focus-composer", ["I"], "Type", true, "agent"),
            binding("toggle-focus", ["Tab", "Shift+Tab"], "Switch focus"),
            binding("switcher", ["Space", "Ctrl+K"], "Commands"),
            binding("escape", ["Escape"], "Conversation", false)],
        pane: [binding("move-left", ["Alt+Left", "Ctrl+Alt+Left"], "Left pane", false),
            binding("move-right", ["Alt+Right", "Ctrl+Alt+Right"], "Right pane", false),
            binding("move-up", ["Alt+Up", "Ctrl+Alt+Up"], "Upper pane", false),
            binding("move-down", ["Alt+Down", "Ctrl+Alt+Down"], "Lower pane", false),
            binding("split-right", ["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"], "Split right", false),
            binding("split-down", ["Alt+S", "Ctrl+Alt+S", "Ctrl+Shift+H"], "Split down", false),
            binding("close-pane", ["Alt+X", "Ctrl+Alt+X", "Ctrl+Shift+W"], "Close pane", false),
            binding("zoom", ["Alt+Z", "Ctrl+Alt+Z", "Ctrl+Shift+Z"], "Zoom pane", false),
            binding("balance", ["Alt+=", "Ctrl+Alt+=", "Ctrl+Shift+="], "Balance panes", false)],
        sidebar: [binding("agent-next", ["J", "Down"], "Next", true, "rows"),
            binding("agent-previous", ["K", "Up"], "Previous", true, "rows"),
            binding("agent-open", ["Return", "Enter"], "Open", true, "rows"),
            binding("agent-search", ["/"], "Search")],
        composer: [binding("send", ["Return"], "Send", true, "send", true),
            binding("newline", ["Shift+Return"], "New line", true, "", true),
            binding("queue", ["Ctrl+Return"], "Queue", true, "send", true)],
        search: [binding("search-next", ["Down"], "Results", true, "rows", true),
            binding("escape", ["Escape"], "Agents")],
        settings: [binding("settings-move", ["Up", "Down"], "Move", true, "", true),
            binding("settings-open", ["Return"], "Change", true, "", true),
            binding("escape", ["Escape"], "Back")],
        updates: [binding("refresh", ["Ctrl+R"], "Refresh"), binding("escape", ["Escape"], "Chats")],
        teams: [binding("escape", ["Escape"], "Chats")],
        modal: [binding("escape", ["Escape"], "Close")],
        blocked: []
    })
    function allowed(entry) {
        return entry.guard === "agent" ? hasAgent : entry.guard === "rows" ? hasRows
            : entry.guard === "send" ? canSend : true;
    }
    function resolve(state) {
        let result = [];
        let seen = {};
        let current = state;
        while (current) {
            for (const entry of states[current] || []) {
                if (seen[entry.action]) continue;
                seen[entry.action] = true;
                if (allowed(entry)) result.push(entry);
            }
            current = parents[current] || "";
        }
        return result;
    }
    readonly property var activeBindings: resolve(contextName)
    readonly property var hints: activeBindings.filter(entry => entry.hint)
    readonly property var shortcuts: {
        let result = [];
        for (const entry of activeBindings) {
            if (entry.native) continue;
            for (const key of entry.keys) result.push({key: key, action: entry.action});
        }
        return result;
    }
}
