//! Every Ctrl+K command and every user setting, each with a one-line
//! description and the other names people search it by, and the one matcher
//! the switcher and the settings search share.
//!
//! An entry's id is the command's action ("split-right") or the settings
//! row's id ("nav-rail"). Ties in a search keep this list's order, so the
//! window's own parts come first, left to right as they sit on screen.
//! `tests::every_command_and_setting_is_described` keeps a new command or
//! setting from shipping without a description and two aliases.

#[derive(Debug)]
pub struct Entry {
    pub id: &'static str,
    pub label: &'static str,
    pub description: &'static str,
    /// Other names: synonyms, verbs, British and American spellings, old labels.
    pub aliases: &'static [&'static str],
    pub group: &'static str,
    /// The actions whose keys reach it (shown beside a setting).
    pub actions: &'static [&'static str],
}

const fn e(id: &'static str, label: &'static str, group: &'static str, description: &'static str, aliases: &'static [&'static str]) -> Entry {
    Entry { id, label, description, aliases, group, actions: &[] }
}

const fn keyed(entry: Entry, actions: &'static [&'static str]) -> Entry {
    Entry { actions, ..entry }
}

/// Words a visibility switch answers to.
macro_rules! shown {
    ($($alias:literal),* $(,)?) => { &[$($alias,)* "hide", "show", "toggle", "on off", "visible", "collapse", "expand"] };
}

pub const ENTRIES: &[Entry] = &[
    // ---- the window's parts, left to right
    e("nav-rail", "Activity bar", "layout", "The icon strip on the far left: Chats, Updates, Teams and Settings.", shown!["navigation rail", "nav rail", "rail", "left bar", "left side", "icons", "icon bar", "activity rail", "sidebar icons", "destinations"]),
    keyed(e("explorer", "Explorer", "layout", "The list of agents and chats beside the conversation.", shown!["sidebar", "side bar", "side panel", "panel", "agents list", "chat list", "left panel"]), &["sidebar"]),
    e("avatar-size", "Agent picture size", "layout", "How large agents' portraits are in the explorer; opens a list of sizes.", &["avatar", "avatars", "portrait", "portraits", "profile picture", "photo", "face", "picture size", "bigger", "smaller"]),
    keyed(e("compact-explorer", "Compact explorer", "layout", "Shows only each chat's avatar and name in the explorer.", &["dense", "condensed", "small rows", "avatar only", "sidebar", "toggle", "density"]), &["toggle-compact"]),
    keyed(e("live-preview", "Explorer live preview", "layout", "Opens the chat under the explorer's cursor while you move through the list.", &["preview", "peek", "follow cursor", "sidebar", "toggle", "browse"]), &["toggle-preview"]),
    keyed(e("workspace-bar", "Workspace bar", "layout", "The strip of workspace tabs along the top.", shown!["workspaces", "tabs", "tab bar", "top bar", "strip"]), &["setting:workspaceBarVisible"]),
    keyed(e("shortcut-bar", "Shortcut bar", "layout", "The key hints along the bottom of the window.", shown!["keybindings", "key bindings", "hints", "status bar", "bottom bar", "shortcuts", "footer"]), &["shortcut-bar"]),
    e("minimal-ui", "Minimal UI", "layout", "Hides chips, switches and other chrome for a quieter window.", shown!["zen", "focus mode", "distraction free", "clean", "simple", "chrome", "declutter"]),
    keyed(e("timestamps", "Timestamps", "chats", "Shows the time beside each message.", shown!["time", "date", "clock", "message times", "stamps"]), &["setting:timestampsVisible"]),
    e("reading-theme", "Reading theme", "appearance", "The colours and typeface of the whole window; opens a list to pick one.", &["theme", "colours", "colors", "color scheme", "colour scheme", "dark mode", "light mode", "font", "typeface", "appearance", "skin", "contrast"]),
    e("font", "Font", "appearance", "The chat's typeface and size for this reading theme; opens the font picker.", &["typeface", "font family", "font size", "text size", "monospace", "serif", "sans"]),
    e("choose-font", "Choose font…", "appearance", "Picks the chat's typeface and size for this reading theme, previewed in the chat.", &["font", "typeface", "font family", "font size", "text size", "monospace", "serif", "sans"]),
    e("reset-font", "Reset font to theme default", "appearance", "Goes back to the reading theme's own typeface and size.", &["font", "typeface", "revert", "default font", "undo font", "restore"]),
    e("ui-scale", "Interface size", "appearance", "How large the whole interface is drawn; opens a list of sizes.", &["zoom", "scale", "size", "bigger", "smaller", "larger", "font size", "text size", "dpi"]),
    e("reduced-motion", "Reduce Motion", "appearance", "Turns off animations and smooth scrolling.", &["animations", "animation", "motion", "accessibility", "reduce animations", "toggle", "still"]),
    // ---- chats
    e("show-when-ready", "Show when ready", "chats", "Shows a reply once it is complete instead of while it streams in.", &["stream", "streaming", "typing", "live text", "wait for answer", "toggle"]),
    keyed(e("activity", "Tool activity", "chats", "How an agent's tool calls show in the chat: grouped, always visible or old ones grouped.", &["tool calls", "tools", "collapse", "expand", "grouping", "group tools", "activity"]), &["tools"]),
    keyed(e("tool-explanations", "Tool explanations (Host)", "chats", "The Host's plain-language explanation of each tool row.", &["explain", "explanations", "plain english", "narration", "tool labels", "toggle"]), &["toggle-explanations"]),
    keyed(e("narration", "Plain-English tools", "experiment", "Describes tool calls in plain English with Spark (uses extra AI).", &["narration", "narrator", "spark", "explain tools", "plain english", "toggle"]), &["tool-narration"]),
    e("tool-detail", "Tool detail", "experiment", "How much the plain-English tool descriptions say; opens a list of levels.", &["detail level", "explanation", "verbosity", "narration", "audience", "explanations"]),
    // ---- startup and agents
    e("new-agent-on-startup", "Start a new agent when opening Clarp", "startup", "Opens the new-agent hub each time Clarp starts.", &["startup", "launch", "on open", "new session", "boot", "toggle"]),
    e("anonymous-agents", "Anonymous agents by default", "agent", "New agents start without a contact's name and persona.", &["anonymous", "nameless", "persona", "identity", "contact", "toggle"]),
    // ---- voice, notifications, keyboard, Host
    keyed(e("spoken-replies", "Spoken replies", "audio", "Reads agents' replies aloud.", &["voice", "mute", "unmute", "speech", "tts", "audio", "sound", "read aloud", "voice replies", "toggle"]), &["mute"]),
    e("voice-provider", "Voice provider", "audio", "The Host's text-to-speech service; opens a list to pick one.", &["tts", "speech", "voice", "elevenlabs", "kokoro", "speaker"]),
    e("voice-fallback", "Voice fallback", "audio", "The voice the Host uses when the main provider fails; opens a list.", &["backup voice", "tts fallback", "secondary", "speech", "voice"]),
    e("pause-mobile-push", "Pause phone alerts while active on desktop", "notifications", "Holds phone notifications while you are using this window.", &["notifications", "notification", "push", "phone", "mobile", "alerts", "quiet", "do not disturb", "toggle"]),
    e("double-press", "Double-press window", "keyboard", "How quickly the second press of a double press (Right Right) must follow; opens a list.", &["double press", "double tap", "double click", "key timing", "delay", "speed", "milliseconds"]),
    e("shared-filesystem", "Shared filesystem", "host", "Lets the app open the Host's files directly (a trusted Host on this machine).", &["files", "local folders", "filesystem", "trusted host", "disk", "toggle"]),
    e("connection", "Host connection", "host", "The Clarp Host this window talks to, and its address and state.", &["host", "server", "connect", "url", "address", "status"]),
    e("orchestrator", "Orchestrator settings", "host", "The orchestrator that routes your messages to agents.", &["orchestrator", "routing", "router", "dispatch", "hands-free"]),
    // ---- commands
    e("edit-keymap", "Customize key bindings", "view", "Opens the editor for every keyboard shortcut.", &["keymap", "shortcuts", "hotkeys", "keyboard", "customise", "rebind", "bindings"]),
    e("next-workspace", "Next workspace", "view", "Switches to the next workspace tab.", &["workspace", "tab", "cycle", "switch workspace"]),
    e("quick-new-agent", "New contact & chat", "agent", "Creates a new contact and opens a chat with it.", &["new session", "hub", "create", "add contact", "new persona"]),
    e("rename-agent", "Rename contact", "agent", "Changes the open contact's name.", &["rename", "name", "title", "relabel", "persona"]),
    e("new", "New session", "agent", "Opens the hub to start a new agent.", &["new agent", "start", "chat", "provider", "contact", "hub", "create"]),
    e("preview-versions", "Preview versions · update or roll back", "settings", "Installs another build of this app, newer or older.", &["previous installs", "rollback", "downgrade", "upgrade", "update", "version"]),
    e("new-contact", "Start an idle contact", "agent", "Starts one of your idle contacts in a new session.", &["new session", "hub", "contact", "wake", "resume"]),
    e("agent-terminal", "Open agent in terminal", "agent", "Opens the agent's native CLI in a terminal.", &["terminal", "cli", "shell", "console", "native"]),
    e("split-right", "Split right", "layout", "Opens a second pane to the right.", &["vertical split", "side by side", "pane", "columns", "divide"]),
    e("split-down", "Split down", "layout", "Opens a second pane below.", &["horizontal split", "stack", "pane", "rows", "divide"]),
    e("close-pane", "Close pane", "layout", "Closes the active pane.", &["remove pane", "unsplit", "merge", "pane"]),
    e("zoom", "Zoom pane", "layout", "Fills the window with the active pane, or restores the split.", &["maximise", "maximize", "fullscreen", "focus pane", "pane"]),
    e("balance", "Balance panes", "layout", "Makes every pane the same size.", &["equalise", "equalize", "even", "resize", "panes"]),
    e("ui-larger", "Larger interface", "view", "Draws the whole interface one step larger.", &["zoom in", "bigger", "increase", "scale up", "font size"]),
    e("ui-smaller", "Smaller interface", "view", "Draws the whole interface one step smaller.", &["zoom out", "shrink", "decrease", "scale down", "font size"]),
    e("ui-reset", "Reset interface size", "view", "Returns the interface to its usual size.", &["zoom reset", "default size", "actual size", "scale"]),
    e("list-all", "Agents: all chats", "view", "Shows every chat in the explorer.", &["sidebar list", "scope", "everything", "back", "all agents"]),
    e("list-unread", "Agents: unread only", "view", "Shows only chats with unread messages in the explorer.", &["sidebar list", "filter", "new", "unread", "scope"]),
    e("list-rooms", "Agent conversations (agent to agent)", "view", "Shows the rooms where agents talk to each other.", &["pairs", "rooms", "sidebar", "agent to agent", "a2a"]),
    e("list-archive", "Archived agents", "view", "Shows archived agents so you can restore them.", &["archive", "restore", "old", "sidebar", "history"]),
    e("link-hints", "Open a link", "view", "Numbers the links on screen; type a number to open one.", &["links", "hints", "url", "urls", "browser", "follow", "vimium", "web"]),
    e("jump-latest", "Jump to latest", "view", "Scrolls the chat to its newest message.", &["bottom", "newest", "scroll", "follow", "end"]),
    e("retry-message", "Retry latest failed message", "view", "Sends the last message that failed again.", &["resend", "send", "delivery", "not delivered", "retry"]),
    e("dismiss-error", "Dismiss conversation error", "view", "Closes the error banner above the chat.", &["clear", "close", "error", "warning", "banner", "voice synthesis failed"]),
    e("dismiss-layout-warning", "Dismiss layout warning", "view", "Closes the warning that another window saved a newer layout.", &["another window", "newer layout", "conflict", "recovery", "close"]),
    e("keep-layout", "Keep this window's layout", "view", "Saves this window's layout over the newer one another window saved.", &["another window", "newer layout", "conflict", "recovery", "save instead"]),
    e("change-directory", "Change directory", "agent", "Starts a new chat in another folder.", &["folder", "workspace", "cwd", "new chat", "path"]),
    e("recent-agents", "Recent agents", "agent", "Lists the agents you opened last, newest first.", &["last", "previous", "switch back", "mru", "history"]),
    e("search-messages", "Search messages", "view", "Finds words in the messages of every chat on this computer and jumps to the message.", &["find", "find in chats", "text", "history", "transcript", "grep", "look up"]),
    e("refresh", "Refresh conversation", "view", "Loads the open chat again from the Host.", &["reload", "update", "sync", "fetch"]),
    e("overview", "Agent overview", "view", "Shows every agent and what it is doing.", &["dashboard", "summary", "all agents", "status", "fleet"]),
    e("chats", "Chats", "destination", "Goes to the chats.", &["conversations", "messages", "home", "main"]),
    e("updates", "Updates", "destination", "Goes to the updates feed.", &["feed", "activity", "news", "attention", "inbox"]),
    e("teams", "Teams", "destination", "Goes to the teams.", &["groups", "team", "crews", "squads"]),
    e("settings", "Settings", "destination", "Opens the settings page.", &["preferences", "options", "configuration", "config", "customise", "customize"]),
    e("next-attention", "Next agent needing attention", "agent", "Opens the next agent that is waiting for you.", &["attention", "waiting", "blocked", "needs me", "next"]),
    e("next-agent", "Next agent", "agent", "Opens the next agent in the list.", &["following", "chat", "switch", "forward", "down"]),
    e("previous-agent", "Previous agent", "agent", "Opens the agent above in the list.", &["back", "chat", "switch", "prior", "up"]),
    e("release-agent", "Release agent", "agent", "Ends the open agent's session and frees its contact.", &["end", "free", "finish", "close agent", "dismiss"]),
    e("stop-agent", "Stop agent", "agent", "Interrupts what the open agent is doing.", &["interrupt", "cancel", "halt", "abort", "kill"]),
    e("talk", "Talk", "audio", "Records your voice and sends it to the open agent.", &["voice", "microphone", "mic", "dictate", "speak", "record", "push to talk"]),
];

pub fn find(id: &str) -> Option<&'static Entry> {
    ENTRIES.iter().find(|entry| entry.id == id)
}

/// The entry's place in the list: ties in a search keep it.
pub fn order(id: &str) -> usize {
    ENTRIES.iter().position(|entry| entry.id == id).unwrap_or(ENTRIES.len())
}

fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty())
}

/// Optimal string alignment distance: insertions, deletions, substitutions
/// and swapped neighbours each cost one.
fn distance(a: &[char], b: &[char]) -> usize {
    let mut rows = vec![vec![0usize; b.len() + 1]; a.len() + 1];
    for (i, row) in rows.iter_mut().enumerate() {
        row[0] = i;
    }
    for j in 0..=b.len() {
        rows[0][j] = j;
    }
    for i in 1..=a.len() {
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            let mut best = (rows[i - 1][j] + 1).min(rows[i][j - 1] + 1).min(rows[i - 1][j - 1] + cost);
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                best = best.min(rows[i - 2][j - 2] + 1);
            }
            rows[i][j] = best;
        }
    }
    rows[a.len()][b.len()]
}

/// A typo of `word` or of how it starts (as long as the term or one letter
/// longer): within one edit, two from seven letters.
fn near(term: &str, word: &str) -> bool {
    let term: Vec<char> = term.chars().collect();
    if term.len() < 4 {
        return false;
    }
    let word: Vec<char> = word.chars().collect();
    let allowed = if term.len() >= 7 { 2 } else { 1 };
    let lengths = [term.len(), term.len() + 1, word.len()];
    lengths.iter().filter(|n| **n > 0 && **n <= word.len()).any(|n| distance(&term, &word[..*n]) <= allowed)
}

/// How well one lowercase term matches: the label first (from its start,
/// then a word's start, then anywhere), then an alias, then the description,
/// then a typo of any of them.
fn term_score(term: &str, label: &str, aliases: &[String], description: &str) -> Option<u32> {
    let starts = |text: &str| words(text).any(|w| w.starts_with(term));
    let score = if label.starts_with(term) {
        1000
    } else if starts(label) {
        900
    } else if label.contains(term) {
        700
    } else if aliases.iter().any(|a| a.starts_with(term) || starts(a)) {
        600
    } else if aliases.iter().any(|a| a.contains(term)) {
        500
    } else if starts(description) {
        300
    } else if description.contains(term) {
        200
    } else if words(label).any(|w| near(term, w)) {
        150
    } else if aliases.iter().any(|a| words(a).any(|w| near(term, w))) {
        120
    } else if words(description).any(|w| near(term, w)) {
        50
    } else {
        return None;
    };
    Some(score)
}

/// How well `query` matches: every word must match somewhere (higher is
/// better); None when one does not. An empty query matches everything.
pub fn score(query: &str, label: &str, aliases: &[&str], description: &str) -> Option<u32> {
    let label = label.to_lowercase();
    let description = description.to_lowercase();
    let aliases: Vec<String> = aliases.iter().map(|a| a.to_lowercase()).collect();
    let terms: Vec<String> = query.to_lowercase().split_whitespace().map(str::to_owned).collect();
    // The whole query as one alias ("left bar") is worth more than its words apart.
    let phrase = if terms.len() > 1 && aliases.iter().any(|a| a.starts_with(&terms.join(" "))) { 200 } else { 0 };
    terms.iter().try_fold(phrase, |total, term| term_score(term, &label, &aliases, &description).map(|s| total + s))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ranked(query: &str) -> Vec<&'static str> {
        let mut hits: Vec<(u32, usize, &str)> =
            ENTRIES.iter().enumerate().filter_map(|(i, e)| score(query, e.label, e.aliases, e.description).map(|s| (s, i, e.id))).collect();
        hits.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        hits.into_iter().map(|(_, _, id)| id).collect()
    }

    fn top3(query: &str, id: &str) {
        let hits = ranked(query);
        assert!(hits.iter().take(3).any(|h| *h == id), "{query:?} must find {id} in the top three: {hits:?}");
    }

    #[test]
    fn every_entry_has_a_description_and_two_aliases() {
        for entry in ENTRIES {
            assert!(!entry.label.is_empty(), "{} has no label", entry.id);
            assert!(entry.description.len() > 10 && !entry.description.contains('\n'), "{} needs a one-line description", entry.id);
            assert!(entry.aliases.len() >= 2, "{} needs at least two aliases", entry.id);
            assert_eq!(ENTRIES.iter().filter(|other| other.id == entry.id).count(), 1, "{} is listed twice", entry.id);
        }
    }

    #[test]
    fn every_setting_on_the_page_is_described() {
        for id in crate::settings_view::IDS {
            assert!(find(id).is_some(), "the setting {id} needs a catalogue entry");
        }
    }

    #[test]
    fn peters_words_find_the_activity_bar() {
        for query in ["hide", "collapse", "toggle", "left bar", "icons", "rail", "activity", "colapse", "hdie", "navigation rail"] {
            top3(query, "nav-rail");
        }
    }

    #[test]
    fn the_label_beats_an_alias_and_an_alias_beats_the_description() {
        assert_eq!(ranked("split")[..2], ["split-right", "split-down"]);
        assert_eq!(ranked("theme")[0], "reading-theme");
        assert_eq!(ranked("colour")[0], "reading-theme", "British spelling");
        assert_eq!(ranked("color")[0], "reading-theme", "American spelling");
        assert_eq!(ranked("mute")[0], "spoken-replies");
        top3("sidebar", "explorer");
        top3("notifications", "pause-mobile-push");
        top3("double tap", "double-press");
        top3("zoom", "ui-scale");
        assert_eq!(ranked("avatar")[0], "avatar-size");
        top3("portrait size", "avatar-size");
    }

    #[test]
    fn every_word_must_match() {
        assert_eq!(ranked("split right"), ["split-right"]);
        assert!(ranked("split banana").is_empty());
        assert!(score("", "Anything", &[], "").is_some());
    }

    #[test]
    fn typos_are_forgiven_only_so_far() {
        assert!(near("colapse", "collapse"));
        assert!(near("hdie", "hide"));
        assert!(near("timstamps", "timestamps"));
        assert!(!near("hid", "hide"), "three letters are matched exactly");
        assert!(!near("zebra", "theme"));
        assert!(!near("hide", "identity"), "a shorter start is not a typo");
    }
}
