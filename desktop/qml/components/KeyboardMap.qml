import QtQuick
import QtCore

// Child bindings override ancestors by action. A blocking state has no parent.
// Both Shortcut dispatch and footer hints consume the same resolved bindings.
QtObject {
    id: root
    property QtObject overrideStore: Settings {id:store;location:StandardPaths.writableLocation(StandardPaths.AppConfigLocation)+"/keymap.ini";category:"keymap";property string encoded:"{}"}
    property var overrides: {try{return JSON.parse(store.encoded);}catch(e){return {};}}
    property string error: ""
    readonly property var editableActions: ["switcher","sidebar","split-right","split-down","zoom","balance","next-workspace"]
    function exportBindings(){return JSON.stringify({version:1,bindings:overrides},null,2);}
    function resetBindings(){store.encoded="{}";error="";}
    function importBindings(text){
        try {
            const value=JSON.parse(text);
            if(!value || typeof value!=="object" || Array.isArray(value) || value.version!==1 || !value.bindings || typeof value.bindings!=="object" || Array.isArray(value.bindings) || Object.keys(value).some(k=>!["version","bindings"].includes(k)))throw Error("Unsupported keymap");
            const next=value.bindings;
            for(const action of Object.keys(next)) {
                if(!editableActions.includes(action)||typeof next[action]!=="string"||!/^Ctrl\+(Alt\+|Shift\+)?[A-Z0-9,]$/.test(next[action]))throw Error("Use a supported action and one Ctrl chord");
                if(["Ctrl+A","Ctrl+C","Ctrl+V","Ctrl+X","Ctrl+Z","Ctrl+Y"].includes(next[action]))throw Error("Reserved text editing key");
            }
            for(const state of Object.keys(states)) {
                const seen={};
                for(const e of resolve(state,next,true))for(const key of e.keys){if(seen[key]&&seen[key]!==e.action)throw Error("Conflict in "+state+": "+key);seen[key]=e.action;}
            }
            store.encoded=JSON.stringify(next);error="";return true;
        }catch(e){error=String(e.message||e);return false;}
    }
    function setBinding(action,key){const next=Object.assign({},overrides);if(key.trim())next[action]=key.trim();else delete next[action];return importBindings(JSON.stringify({version:1,bindings:next}));}
    property string contextName: "pane"
    property bool hasAgent: false
    property bool hasRows: false
    property bool canSend: false
    property bool hasAttention: false
    readonly property var parents: ({main: "root", workspace: "main", navigation: "workspace",
        pane: "navigation", sidebar: "navigation", composer: "workspace", search: "workspace",
        settings: "main", updates: "main", teams: "main", modal: "root", launch: "root", blocked: "root"})
    function binding(action, keys, label, hint = true, guard = "", native = false) {
        return {action: action, keys: keys, label: label, hint: hint, guard: guard, native: native};
    }
    readonly property var states: ({
        root: [],
        main: [binding("edit-keymap", ["Ctrl+Alt+,"], "Key bindings", false),
            binding("next-workspace", ["Ctrl+Alt+W"], "Next workspace", false),
            binding("update-preview", ["Ctrl+Alt+U"], "Update", false),
            binding("next-attention", ["Ctrl+J"], "Next attention", true, "attention"),
            binding("change-directory", ["Ctrl+Alt+D"], "Change directory", false),
            binding("switcher", ["Ctrl+K"], "Commands"),
            binding("sidebar", ["Ctrl+B"], "Show/hide sidebar", false),
            binding("shortcut-bar", ["Ctrl+Shift+K"], "Show/hide keybindings", false),
            binding("quick-new-agent", ["Ctrl+Shift+N"], "New contact & chat", false),
            binding("rename-agent", ["F2"], "Rename contact", false),
            binding("new", ["Ctrl+N"], "New agent", false),
            binding("new-contact", ["Ctrl+Alt+N"], "Start contact", false),
            binding("settings", ["Ctrl+,", "Ctrl+4"], "Settings", false),
            binding("chats", ["Ctrl+1"], "Chats", false), binding("updates", ["Ctrl+2"], "Updates", false),
            binding("teams", ["Ctrl+3"], "Teams", false), binding("refresh", ["Ctrl+R"], "Refresh", false),
            binding("mute", ["Ctrl+M"], "Mute", false), binding("overview", ["Ctrl+Shift+O"], "Overview", false),
            binding("tools", ["Ctrl+Shift+T"], "Tools", false),
            binding("ui-larger", ["Ctrl+="], "Larger", false), binding("ui-smaller", ["Ctrl+-"], "Smaller", false),
            binding("ui-reset", ["Ctrl+0"], "Reset scale", false)],
        workspace: [binding("retry-message", ["Ctrl+Alt+R"], "Retry failed message", false, "agent"),
            binding("jump-latest", ["Ctrl+End"], "Latest", true, "agent"),
            binding("assign-agent", ["Ctrl+A"], "Assign contact", false, "agent"),
            binding("auto-assign-agent", ["Ctrl+Shift+A"], "Auto assign", false, "agent"),
            binding("escape", ["Escape"], "Navigate"),
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
            binding("stop-agent", ["Ctrl+.", "Ctrl+C"], "Stop", false, "agent"),
            binding("talk", ["Ctrl+Shift+Space"], "Talk", false, "agent")],
        navigation: [binding("next-attention", ["N", "Ctrl+J"], "Next attention", true, "attention"),
            binding("focus-sidebar", ["E"], "Agents"),
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
        composer: [binding("jump-latest", ["Ctrl+End"], "Latest", true, "agent", true),
            binding("send", ["Return"], "Send", true, "send", true),
            binding("newline", ["Shift+Return"], "New line", true, "", true),
            binding("queue", ["Ctrl+Return"], "Queue", true, "send", true)],
        search: [binding("search-next", ["Down"], "Results", true, "rows", true),
            binding("escape", ["Escape"], "Agents")],
        settings: [binding("settings-move", ["Up", "Down"], "Move", true, "", true),
            binding("settings-open", ["Return"], "Change", true, "", true),
            binding("escape", ["Escape"], "Back")],
        updates: [binding("refresh", ["Ctrl+R"], "Refresh"), binding("escape", ["Escape"], "Chats")],
        teams: [binding("escape", ["Escape"], "Chats")],
        launch: [binding("switcher", ["Ctrl+K"], "Commands"),
            binding("change-directory", ["Ctrl+Alt+D"], "Change directory"),
            binding("escape", ["Escape"], "Back")],
        modal: [binding("escape", ["Escape"], "Close")],
        blocked: []
    })
    function allowed(entry) {
        return entry.guard === "attention" ? hasAttention : entry.guard === "agent" ? hasAgent : entry.guard === "rows" ? hasRows
            : entry.guard === "send" ? canSend : true;
    }
    function resolve(state, custom=overrides, ignoreGuards=false) {
        let result = [];
        let seen = {};
        let current = state;
        while (current) {
            for (const entry of states[current] || []) {
                if (seen[entry.action]) continue;
                seen[entry.action] = true;
                if (ignoreGuards || allowed(entry)) result.push(custom[entry.action] ? Object.assign({},entry,{keys:[custom[entry.action]]}) : entry);
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
