//! The keyboard map (KeyboardMap.qml): bindings per focus state, where a
//! child state inherits its ancestors' bindings and overrides them by
//! action. The shortcut dispatcher and the shortcut bar's hints read the
//! same resolved list.
//!
//! The user's own bindings are kept in settings (`keymap/bindings`) as
//! `{action: {context: {"add": [keys], "remove": [keys]}}}`: keys added to
//! or taken from the defaults in that context and every context under it.
//! A key is a chord ("Ctrl+J") or a double press of one ("Right Right").
//! Typing keys bound further out never reach a text field (the composer,
//! the explorer's search); bound there itself, they do.

use std::collections::{BTreeMap, HashSet};
use std::time::{Duration, Instant};

use clarp_core::tr;
use serde_json::{Map, Value, json};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Guard {
    None,
    /// Something needs attention.
    Attention,
    /// An agent's chat (not a pair room) is open.
    Agent,
    /// The list has rows.
    Rows,
    /// The composer can send.
    Send,
    /// The open agent is working (thinking, a tool, compacting).
    Busy,
    /// A voice reply is playing.
    Playing,
    /// The reader has scrolled up from the latest message.
    Behind,
    /// Some chat in the explorer has sub-agents to fold or unfold.
    Folds,
    /// The open chat shows artifact cards.
    Artifacts,
    /// The keyboard is on an artifact card.
    Artifact,
    /// Another window saved a newer layout (the conflict bar shows).
    LayoutWarning,
    /// The selected agent runs background jobs or helpers.
    Work,
    /// The open chat's queue strip lists a message Send now can hand over.
    Queued,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub action: &'static str,
    pub keys: Vec<String>,
    /// In the app's language, made when the bindings are resolved.
    pub label: String,
    /// Shown in the shortcut bar.
    pub hint: bool,
    pub guard: Guard,
    /// Shown in the bar only while this holds too (it runs whenever
    /// `guard` does).
    pub hint_guard: Guard,
    /// Handled by the focused control itself (the composer's Return); the
    /// dispatcher lets it through.
    pub native: bool,
}

fn binding(action: &'static str, keys: &[&str], label: String, hint: bool, guard: Guard, native: bool) -> Binding {
    Binding { action, keys: keys.iter().map(|k| (*k).to_owned()).collect(), label, hint, guard, hint_guard: Guard::None, native }
}

impl Binding {
    fn hinted_while(self, guard: Guard) -> Self {
        Self { hint: true, hint_guard: guard, ..self }
    }
}

/// Text editing keys no binding may take.
const RESERVED: &[&str] = &["Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+Y"];

fn parent(state: &str) -> Option<&'static str> {
    Some(match state {
        "main" => "root",
        "workspace" | "settings" | "settings-search" | "updates" | "teams" => "main",
        "navigation" | "composer" | "search" => "workspace",
        "pane" | "sidebar" => "navigation",
        "modal" | "launch" | "blocked" | "hints" => "root",
        // The agent profile: Escape closes it, as any dialog.
        "profile" => "modal",
        _ => return None,
    })
}

fn state(name: &str) -> Vec<Binding> {
    use Guard::{Agent, Artifact, Artifacts, Attention, Behind, Busy, Folds, LayoutWarning, None as Always, Playing, Queued, Rows, Send, Work};
    let b = binding;
    match name {
        "main" => vec![
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the editor for every keyboard shortcut.
            b("edit-keymap", &["Ctrl+Alt+,"], tr!("Key bindings"), false, Always, false),
            // The tab bar's workspaces, as a browser's tabs.
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens a new workspace tab with its own pane layout.
            b("new-workspace", &["Ctrl+T"], tr!("New tab"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It switches to the next workspace tab.
            b("next-workspace", &["Ctrl+Tab", "Ctrl+Alt+W"], tr!("Next tab"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It switches to the workspace tab before this one.
            b("previous-workspace", &["Ctrl+Shift+Tab"], tr!("Previous tab"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It opens the app updates (install another build)
            b("update-preview", &["Ctrl+Alt+U"], tr!("verb" => "Update"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the next agent that is waiting for you.
            b("next-attention", &["Ctrl+J"], tr!("Next attention"), true, Attention, false),
            // No keys of their own: for the user to bind (and the switcher).
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the next agent in the list.
            b("next-agent", &[], tr!("Next agent"), false, Rows, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the agent above in the list.
            b("previous-agent", &[], tr!("Previous agent"), false, Rows, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It the orchestrator that routes your messages to agents.
            b("orchestrator", &[], tr!("Orchestrator settings"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It picks the chat's typeface and size for this reading theme, previewed in the chat.
            b("choose-font", &[], tr!("Choose font…"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It goes back to the reading theme's own typeface and size.
            b("reset-font", &[], tr!("Reset font to theme default"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It the Clarp Host this window talks to, and its address and state.
            b("connection", &[], tr!("Host connection"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows every chat in the explorer.
            b("list-all", &[], tr!("Agents: all chats"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows only chats with unread messages in the explorer.
            b("list-unread", &[], tr!("Agents: unread only"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows only the chats whose agent is working now in the explorer.
            b("list-working", &[], tr!("Agents: working"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows only the chats waiting for you: unread, asking a question or holding a decision. Snoozed chats stay out.
            b("list-needs-you", &[], tr!("Agents: needs you"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows only the chats that are idle and read in the explorer.
            b("list-done", &[], tr!("Agents: done"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the chats you snoozed, with when each comes back.
            b("list-snoozed", &[], tr!("Agents: snoozed"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It takes the chat out of Needs you until it gets a new message. Kept on this device.
            b("snooze-chat", &[], tr!("Snooze chat"), false, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It takes the chat out of Needs you for an hour. Kept on this device.
            b("snooze-chat-hour", &[], tr!("Snooze chat for an hour"), false, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It takes the chat out of Needs you until 08:00 tomorrow. Kept on this device.
            b("snooze-chat-morning", &[], tr!("Snooze chat until morning"), false, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It ends the chat's snooze: it is back in Needs you if it waits for you.
            b("wake-chat", &[], tr!("Wake chat"), false, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the rooms where agents talk to each other.
            b("list-rooms", &[], tr!("Agent conversations"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows archived agents so you can restore them.
            b("list-archive", &[], tr!("Archived agents"), false, Always, false),
            // Everything about the open agent, from anywhere, typing
            // included (no text field uses Ctrl+I).
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the open agent's profile: its artifacts and images, files, terminal, voice, plan, prompt history, model and backend.
            b("agent-profile", &["Ctrl+I"], tr!("Agent profile"), true, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the open chat's queue of waiting messages
            b("manage-queue", &[], tr!("Message queue"), false, Agent, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It hides the strip of waiting messages above the composer, or shows it again (the setting Show the queue).
            b("toggle-queue", &[], tr!("Show/hide queue"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It closes the error banner above the chat.
            b("dismiss-error", &[], tr!("Dismiss conversation error"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It closes the warning that another window saved a newer layout.
            b("dismiss-layout-warning", &[], tr!("Dismiss layout warning"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It turns plain-English tool explanations on or off
            b("tool-narration", &[], tr!("Plain-English tools"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It turns the times beside messages on or off
            b("setting:timestampsVisible", &[], tr!("Timestamps on/off"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows or hides the workspace tab bar
            b("setting:workspaceBarVisible", &[], tr!("Workspace bar on/off"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It starts a new chat in another folder.
            b("change-directory", &["Ctrl+Alt+D"], tr!("Change directory"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the command palette
            b("switcher", &["Ctrl+K"], tr!("Commands"), true, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows or hides the explorer (sidebar)
            b("sidebar", &["Ctrl+B"], tr!("Show/hide sidebar"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It the key hints along the bottom of the window.
            b("shortcut-bar", &["Ctrl+Shift+K"], tr!("Show/hide keybindings"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It creates a new contact and opens a chat with it.
            b("quick-new-agent", &["Ctrl+Shift+N"], tr!("New contact & chat"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It changes the open contact's name.
            b("rename-agent", &["F2"], tr!("Rename contact"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the hub to start a new agent.
            b("new", &["Ctrl+N"], tr!("New agent"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It starts one of your idle contacts in a new session.
            b("new-contact", &["Ctrl+Alt+N"], tr!("Start contact"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the settings page.
            b("settings", &["Ctrl+,", "Ctrl+4"], tr!("Settings"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It goes to the chats.
            b("chats", &["Ctrl+1"], tr!("Chats"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It goes to the updates feed.
            b("updates", &["Ctrl+2"], tr!("Updates"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It goes to the teams.
            b("teams", &["Ctrl+3"], tr!("Teams"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action (noun), in the shortcut bar, Keys help (?) and the key-binding editor. It lists the agents you opened last, newest first.
            b("recent-agents", &["Ctrl+R"], tr!("keys" => "Recent"), true, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It finds words in the messages of every chat (the Host searches them all where it can, else this computer's chats) and jumps to the message.
            b("search-messages", &["Ctrl+F"], tr!("Search messages"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It loads the open chat again from the Host.
            b("refresh", &["F5"], tr!("verb" => "Refresh"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It fetches the agent list from the Host now, also while it is retrying after a failure.
            b("refresh-agents", &["Shift+F5"], tr!("Refresh agents"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It turns spoken replies off or on
            b("mute", &["Ctrl+M"], tr!("verb" => "Mute"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows every agent and what it is doing.
            b("overview", &["Ctrl+Shift+O"], tr!("Overview"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows how tool calls appear in the chat
            b("tools", &["Ctrl+Shift+T"], tr!("Tools"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It turns the Host's tool explanations on or off
            b("toggle-explanations", &["Ctrl+Shift+X"], tr!("Tool explanations"), false, Always, false),
            // The chat's zoom on the browser's keys; the whole window's
            // scale (the chats with it) on Ctrl+Alt+PageUp/PageDown/0.
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It draws the chats one step (10%) larger: text, code, cards and spacing, apart from the rest of the window.
            b("chat-zoom-in", &["Ctrl+=", "Ctrl++"], tr!("Zoom in"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It draws the chats one step (10%) smaller, apart from the rest of the window.
            b("chat-zoom-out", &["Ctrl+-"], tr!("Zoom out"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It draws the chats at 100% again.
            b("chat-zoom-reset", &["Ctrl+0"], tr!("Reset zoom"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It draws the whole window one step larger, the chats included.
            b("ui-larger", &["Ctrl+Alt+PageUp"], tr!("Larger"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It draws the whole window one step smaller, the chats included.
            b("ui-smaller", &["Ctrl+Alt+PageDown"], tr!("Smaller"), false, Always, false),
            // i18n: Keys (key context “Everywhere”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It returns the whole window to its usual scale.
            b("ui-reset", &["Ctrl+Alt+0"], tr!("Reset scale"), false, Always, false),
        ],
        "workspace" => vec![
            // Ctrl+E / Ctrl+H reach the explorer and the chat from anywhere,
            // typing included (no Escape first).
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the explorer (the list of chats)
            b("focus-sidebar", &["Ctrl+E"], tr!("Explorer"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the chat
            b("focus-pane", &["Ctrl+H"], tr!("Chat"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It sends the last message that failed again.
            b("retry-message", &["Ctrl+Alt+R"], tr!("Retry failed message"), false, Agent, false),
            // i18n: Keys (key context “Chats”): the name of a key's action (noun), in the shortcut bar, Keys help (?) and the key-binding editor. It scrolls the chat to its newest message.
            b("jump-latest", &["Ctrl+End"], tr!("keys" => "Latest"), true, Behind, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It lets Clarp pick the agent for the message
            b("auto-assign-agent", &["Ctrl+Shift+A"], tr!("Auto assign"), false, Agent, false),
            // The queue strip's first Send now, from the chat or the composer.
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It hands the open chat's first waiting message to the agent now: read into its running turn where the Host offers that (Steer now), else sent ahead of the queue.
            b("send-queued", &["Ctrl+Shift+Return"], tr!("Send now"), true, Queued, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("Navigate"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It saves this window's layout over the newer one another window saved.
            b("keep-layout", &["Ctrl+Shift+S"], tr!("Keep this layout"), true, LayoutWarning, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane on the left
            b("move-left", &["Ctrl+Alt+Left"], tr!("Left pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane on the right
            b("move-right", &["Ctrl+Alt+Right"], tr!("Right pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane above
            b("move-up", &["Ctrl+Alt+Up"], tr!("Upper pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane below
            b("move-down", &["Ctrl+Alt+Down"], tr!("Lower pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens a second pane to the right.
            b("split-right", &["Ctrl+Alt+V"], tr!("Split right"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens a second pane below.
            b("split-down", &["Ctrl+Alt+S"], tr!("Split down"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It closes the active pane.
            b("close-pane", &["Ctrl+Alt+X"], tr!("Close pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It fills the window with the active pane, or restores the split.
            b("zoom", &["Ctrl+Alt+Z"], tr!("Zoom pane"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It makes every pane the same size.
            b("balance", &["Ctrl+Alt+="], tr!("Balance panes"), false, Always, false),
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the agent's CLI, run by its Host, in a terminal.
            b("agent-terminal", &["Ctrl+Alt+T"], tr!("Terminal"), false, Agent, false),
            // i18n: Keys (key context “Chats”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It ends the open agent's session and frees its contact.
            b("release-agent", &["Ctrl+Shift+R"], tr!("verb" => "Release"), false, Agent, false),
            // Stops the agent from anywhere; the bar names it while it works.
            // i18n: Keys (key context “Chats”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It interrupts what the open agent is doing.
            b("stop-agent", &["Ctrl+.", "Ctrl+C"], tr!("verb" => "Stop"), false, Agent, false).hinted_while(Busy),
            // i18n: Keys (key context “Chats”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It records your voice and sends it to the open agent.
            b("talk", &["Ctrl+Shift+Space"], tr!("verb" => "Talk"), false, Agent, false),
            // Link hints, from the composer too.
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It numbers the links on screen; type a number to open one.
            b("link-hints", &["Ctrl+L"], tr!("Open a link"), false, Always, false),
            // The selected agent's jobs and helpers: output, stop. The bar
            // names it while something runs.
            // i18n: Keys (key context “Chats”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job.
            b("agent-processes", &["Ctrl+Shift+P"], tr!("Processes"), false, Agent, false).hinted_while(Work),
        ],
        "navigation" => vec![
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the next agent that is waiting for you.
            b("next-attention", &["N", "Ctrl+J"], tr!("Next attention"), true, Attention, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the explorer (the list of chats)
            b("focus-sidebar", &["E", "Ctrl+E"], tr!("Explorer"), false, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the chat
            b("focus-pane", &["H", "Ctrl+H"], tr!("Chat"), false, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard in the composer to type
            b("focus-composer", &["I"], tr!("verb" => "Insert"), false, Agent, false),
            // The keys that matter where the keyboard is; never in a text
            // field, where ? types.
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the keys that matter where the keyboard is, your own bindings included.
            b("help-keys", &["?"], tr!("Keys"), true, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard between the explorer and the chat
            b("toggle-focus", &["Tab", "Shift+Tab"], tr!("Switch focus"), false, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the command palette
            b("switcher", &["Space", "Ctrl+K"], tr!("Commands"), true, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It numbers the links on screen; type a number to open one.
            b("link-hints", &["F", "Ctrl+L"], tr!("Open a link"), false, Always, false),
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("Chat"), false, Always, false),
            // Not while typing, where Ctrl+W deletes a word in a terminal.
            // i18n: Keys (key context “Chat and explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It closes this workspace tab; its chats stay in the explorer.
            b("close-workspace", &["Ctrl+W"], tr!("Close tab"), false, Always, false),
        ],
        "pane" => vec![
            // The chat's artifact cards: K from the chat is the lowest card on screen.
            // Capital J and K too: vim mode's j and k scroll.
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the chat's cards (the lowest one on screen)
            b("artifact-previous", &["K", "Shift+K"], tr!("Cards"), true, Artifacts, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the next card in the chat
            b("artifact-next", &["J", "Shift+J"], tr!("Next card"), false, Artifacts, false),
            // O, not Enter: Enter belongs to the composer, where a card's
            // keys never reach.
            // i18n: Keys (key context “Chat”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It opens the card under the keyboard
            b("artifact-open", &["O"], tr!("verb" => "Open"), true, Artifact, false),
            // An HTML report's whole page, pictures and all, in the browser.
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens an HTML report's whole page, pictures and layout included, in a new browser window; Clarp shows plain ones as a quick preview.
            b("open-in-browser", &["B"], tr!("Open in browser"), false, Artifact, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It picks an answer on the card by its number
            b("artifact-choose", &["1", "2", "3", "4", "5", "6", "7", "8", "9"], tr!("Choose"), false, Artifact, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It discards the card under the keyboard
            b("artifact-discard", &["Delete"], tr!("Discard"), false, Artifact, false),
            // A gallery's tiles, an audio clip's position; S stops a clip.
            // i18n: Keys (key context “Chat”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It steps back in a gallery's pictures or an audio clip
            b("artifact-back", &["Left"], tr!("verb" => "Back"), false, Artifact, false),
            // i18n: Keys (key context “Chat”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It steps forward in a gallery's pictures or an audio clip
            b("artifact-forward", &["Right"], tr!("verb" => "Forward"), false, Artifact, false),
            // i18n: Keys (key context “Chat”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It stops the audio clip that is playing
            b("artifact-stop", &["S"], tr!("verb" => "Stop"), false, Artifact, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane on the left
            b("move-left", &["Alt+Left", "Ctrl+Alt+Left"], tr!("Left pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane on the right
            b("move-right", &["Alt+Right", "Ctrl+Alt+Right"], tr!("Right pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane above
            b("move-up", &["Alt+Up", "Ctrl+Alt+Up"], tr!("Upper pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard to the pane below
            b("move-down", &["Alt+Down", "Ctrl+Alt+Down"], tr!("Lower pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens a second pane to the right.
            b("split-right", &["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"], tr!("Split right"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens a second pane below.
            b("split-down", &["Alt+S", "Ctrl+Alt+S", "Ctrl+Shift+H"], tr!("Split down"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It closes the active pane.
            b("close-pane", &["Alt+X", "Ctrl+Alt+X", "Ctrl+Shift+W"], tr!("Close pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It fills the window with the active pane, or restores the split.
            b("zoom", &["Alt+Z", "Ctrl+Alt+Z", "Ctrl+Shift+Z"], tr!("Zoom pane"), false, Always, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It makes every pane the same size.
            b("balance", &["Alt+=", "Ctrl+Alt+=", "Ctrl+Shift+="], tr!("Balance panes"), false, Always, false),
            // Slint's rich text cannot be selected: the chat as plain text.
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the open chat as plain text to select from with the mouse or Shift+arrows and copy with Ctrl+C; a message's own select icon shows just that message.
            b("select-text", &["V"], tr!("Select text"), false, Agent, false),
            // Conversation view (#702): W opens or folds the work of the
            // message the keyboard is on (or the latest turn's), as a click
            // on its wrench or a right-click does; C shows the chat the
            // other way, as its own choice.
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens or folds the work of a finished turn in Conversation view (the message the keyboard is on, else the latest turn), or the running turn's Running box.
            b("show-work", &["W"], tr!("Show work"), false, Agent, false),
            // i18n: Keys (key context “Chat”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the open chat the other way: Conversation (your messages, the replies, cards; the work behind Show work) or Session (everything as it ran). On a Host that offers it.
            b("chat-view", &["C"], tr!("Conversation / Session"), false, Agent, false),
        ],
        "sidebar" => vec![
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the cursor to the next chat in the explorer
            b("agent-next", &["J", "Down"], tr!("Next"), false, Rows, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the cursor to the chat above in the explorer
            b("agent-previous", &["K", "Up"], tr!("Previous"), false, Rows, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It opens the chat under the cursor
            b("agent-open", &["Return", "Enter"], tr!("verb" => "Open"), false, Rows, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard in the explorer's search field
            b("agent-search", &["/"], tr!("verb" => "Search"), true, Always, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action (noun), in the shortcut bar, Keys help (?) and the key-binding editor. It turns the explorer live preview on or off
            b("toggle-preview", &["P"], tr!("keys" => "Preview"), true, Always, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It switches the explorer to its compact view and back
            b("toggle-compact", &["V"], tr!("Compact view"), true, Always, false),
            // The cursor's chat: snoozed until its next message, or woken.
            // i18n: Keys (key context “Explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It snoozes the chat until its next message, or wakes it when snoozed.
            b("snooze-toggle", &["S"], tr!("verb" => "Snooze"), true, Rows, false),
            // The row's own badge, by key (P is the preview).
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job.
            b("agent-processes", &["Shift+P", "Ctrl+Shift+P"], tr!("Processes"), false, Agent, false).hinted_while(Work),
            // i18n: Keys (key context “Explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It folds a chat's sub-agents in the explorer
            b("fold", &["Left"], tr!("verb" => "Fold"), true, Folds, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It unfolds a chat's sub-agents in the explorer
            b("unfold", &["L", "Right"], tr!("verb" => "Unfold"), true, Folds, false),
            // The explorer's header buttons, shown while it has the keyboard.
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the hub to start a new agent.
            b("new", &["Ctrl+N"], tr!("New agent"), true, Always, false),
            // i18n: Keys (key context “Explorer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows or hides the explorer (sidebar)
            b("sidebar", &["Ctrl+B"], tr!("Hide sidebar"), true, Always, false),
        ],
        "composer" => vec![
            // i18n: Keys (key context “Composer”): the name of a key's action (noun), in the shortcut bar, Keys help (?) and the key-binding editor. It scrolls the chat to its newest message.
            b("jump-latest", &["Ctrl+End"], tr!("keys" => "Latest"), true, Behind, true),
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It sends the message
            b("send", &["Return"], tr!("Send"), false, Send, true),
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It starts a new line in the message
            b("newline", &["Shift+Return"], tr!("New line"), false, Always, true),
            // i18n: Keys (key context “Composer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It queues the message to send after the running turn
            b("queue", &["Ctrl+Return"], tr!("verb" => "Queue"), true, Busy, true),
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the chat
            b("focus-pane", &["Ctrl+H"], tr!("Chat"), false, Always, false),
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It puts the keyboard on the explorer (the list of chats)
            b("focus-sidebar", &["Ctrl+E"], tr!("Explorer"), false, Always, false),
            // The composer has no buttons: its actions are keys, shown here.
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It attaches a file to the message
            b("attach", &["Ctrl+Shift+O"], tr!("Attach"), true, Agent, true),
            // i18n: Keys (key context “Composer”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It interrupts what the open agent is doing.
            b("stop-agent", &["Ctrl+."], tr!("verb" => "Stop"), true, Busy, false),
            // i18n: Keys (key context “Composer”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It stops the voice reply that is playing
            b("silence", &["Ctrl+Shift+M"], tr!("Stop voice"), true, Playing, false),
        ],
        "search" => vec![
            // i18n: Keys (key context “Explorer search”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard from the search field to the results
            b("search-next", &["Down"], tr!("Results"), false, Rows, true),
            // i18n: Keys (key context “Explorer search”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("Explorer"), false, Always, false),
        ],
        "settings" => vec![
            // i18n: Keys (key context “Settings”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It moves between settings
            b("settings-move", &["Up", "Down"], tr!("verb" => "Move"), true, Always, true),
            // i18n: Keys (key context “Settings”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It changes the setting under the cursor
            b("settings-open", &["Return"], tr!("verb" => "Change"), true, Always, true),
            // i18n: Keys (key context “Settings”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It searches the settings
            b("settings-search", &["/"], tr!("verb" => "Search"), true, Always, false),
            // i18n: Keys (key context “Settings”): the name of a key's action (noun), in the shortcut bar, Keys help (?) and the key-binding editor. It puts the setting under the cursor back to its default
            b("settings-reset", &["Delete"], tr!("keys" => "Default"), true, Always, false),
            // i18n: Keys (key context “Settings”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("verb" => "Back"), true, Always, false),
            // i18n: Keys (key context “Settings”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the keys that matter where the keyboard is, your own bindings included.
            b("help-keys", &["?"], tr!("Keys"), true, Always, false),
        ],
        // The settings page's search field.
        "settings-search" => vec![
            // i18n: Keys (key context “Settings search”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves the keyboard from the settings search to the results
            b("settings-results", &["Down", "Return"], tr!("Results"), true, Always, false),
            // i18n: Keys (key context “Settings search”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It empties the settings search
            b("settings-search-cancel", &["Escape"], tr!("verb" => "Clear"), true, Always, false),
        ],
        "updates" => vec![
            // i18n: Keys (key context “Updates”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It loads the open chat again from the Host.
            b("refresh", &["F5"], tr!("verb" => "Refresh"), true, Always, false),
            // Every job, with its output and a stop.
            // i18n: Keys (key context “Updates”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job.
            b("agent-processes", &["Ctrl+Shift+P"], tr!("Jobs"), true, Always, false),
            // i18n: Keys (key context “Updates”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("Chats"), true, Always, false),
            // i18n: Keys (key context “Updates”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the keys that matter where the keyboard is, your own bindings included.
            b("help-keys", &["?"], tr!("Keys"), true, Always, false),
        ],
        "teams" => vec![
            // i18n: Keys (key context “Teams”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("Chats"), true, Always, false),
            // i18n: Keys (key context “Teams”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It shows the keys that matter where the keyboard is, your own bindings included.
            b("help-keys", &["?"], tr!("Keys"), true, Always, false),
        ],
        "launch" => vec![
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It opens the command palette
            b("switcher", &["Ctrl+K"], tr!("Commands"), true, Always, false),
            // The hub's own keys (it handles them; listed here for the bar).
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: picks the backend (Claude, Codex, ...)
            b("launch-provider", &["Ctrl+Left"], tr!("Backend"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: picks the model
            b("launch-model", &["Ctrl+M"], tr!("Model"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: picks the reasoning effort
            b("launch-effort", &["Ctrl+E"], tr!("Effort"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: picks the folder the agent works in
            b("launch-directory", &["Ctrl+D"], tr!("Directory"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: creates a new contact
            b("launch-new-contact", &["Ctrl+N"], tr!("New contact"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It new agent hub: shows every option
            b("launch-show-all", &["Ctrl+T"], tr!("Show all"), true, Always, true),
            // i18n: Keys (key context “New agent hub”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It starts a new chat in another folder.
            b("change-directory", &["Ctrl+Alt+D"], tr!("Change directory"), false, Always, false),
            // i18n: Keys (key context “New agent hub”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("verb" => "Back"), true, Always, false),
        ],
        // i18n: Keys (key context “Dialogs”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
        "modal" => vec![b("escape", &["Escape"], tr!("verb" => "Close"), true, Always, false)],
        // The agent profile's Backend picker (#720): keys move, pick and step back.
        "transit" => vec![
            // i18n: Keys (key context “Backend picker”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the next backend in the picker
            b("transit-down", &["Down", "J"], tr!("Next"), true, Always, false),
            // i18n: Keys (key context “Backend picker”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the backend above in the picker
            b("transit-up", &["Up", "K"], tr!("Previous"), true, Always, false),
            // i18n: Keys (key context “Backend picker”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It chooses the backend under the cursor
            b("transit-enter", &["Return"], tr!("Choose"), true, Always, false),
            // i18n: Keys (key context “Backend picker”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It leaves where the keyboard is, back to the place this label names
            b("escape", &["Escape"], tr!("verb" => "Back"), true, Always, false),
        ],
        // The agent profile from the keyboard: a cursor walks its rows, Tab its
        // cards; a digit jumps to a section (the cards' titles show which);
        // Enter acts on the row, Left and Right change a choice.
        "profile" => vec![
            // i18n: Keys (key context “Agent profile”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It jumps to a section of the agent profile by its number
            b("profile-section", &["1", "2", "3", "4", "5", "6", "7", "8", "9"], tr!("Sections"), true, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the next row of the agent profile
            b("profile-down", &["Down", "J"], tr!("Next"), false, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the row above in the agent profile
            b("profile-up", &["Up", "K"], tr!("Previous"), false, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the next card of the agent profile
            b("profile-next-card", &["Tab"], tr!("Next card"), false, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It moves to the card before in the agent profile
            b("profile-previous-card", &["Shift+Tab"], tr!("Previous card"), false, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It acts on the row of the agent profile (opens it)
            b("profile-enter", &["Return"], tr!("verb" => "Open"), true, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It changes the choice on the row
            b("profile-left", &["Left"], tr!("verb" => "Change"), false, Always, false),
            // i18n: Keys (key context “Agent profile”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It changes the choice on the row
            b("profile-right", &["Right"], tr!("verb" => "Change"), false, Always, false),
        ],
        // Link hints show: digits pick a link (the bar says which), Enter
        // opens a number that also starts a longer one; any other key
        // cancels.
        "hints" => vec![
            // i18n: Keys (key context “Link numbers”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It opens the link with the typed number
            b("hint-digit", &["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], tr!("verb" => "Open"), true, Always, false),
            // i18n: Keys (key context “Link numbers”): the name of a key's action (verb, an action), in the shortcut bar, Keys help (?) and the key-binding editor. It opens the link with the number typed so far
            b("hint-open", &["Return"], tr!("verb" => "Open"), false, Always, false),
            // i18n: Keys (key context “Link numbers”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It removes the last digit typed while link numbers show
            b("hint-back", &["Backspace"], tr!("Undo digit"), false, Always, false),
            // i18n: Keys (key context “Link numbers”): the name of a key's action, in the shortcut bar, Keys help (?) and the key-binding editor. It hides the link numbers
            b("hint-cancel", &["Escape"], tr!("Cancel"), true, Always, false),
        ],
        _ => Vec::new(),
    }
}

pub const STATES: &[&str] = &["root", "main", "workspace", "navigation", "pane", "sidebar", "composer", "search", "settings", "settings-search", "updates", "teams", "launch", "modal", "profile", "transit", "blocked", "hints"];

/// What the guards test.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Facts {
    pub attention: bool,
    pub agent: bool,
    pub rows: bool,
    pub can_send: bool,
    pub busy: bool,
    pub playing: bool,
    pub behind: bool,
    pub folds: bool,
    pub artifacts: bool,
    pub artifact: bool,
    pub layout_warning: bool,
    pub work: bool,
    pub queued: bool,
}

impl Facts {
    fn allows(self, guard: Guard) -> bool {
        match guard {
            Guard::None => true,
            Guard::Attention => self.attention,
            Guard::Agent => self.agent,
            Guard::Rows => self.rows,
            Guard::Send => self.can_send,
            Guard::Busy => self.busy,
            Guard::Playing => self.playing,
            Guard::Behind => self.behind,
            Guard::Folds => self.folds,
            Guard::Artifacts => self.artifacts,
            Guard::Artifact => self.artifact,
            Guard::LayoutWarning => self.layout_warning,
            Guard::Work => self.work,
            Guard::Queued => self.queued,
        }
    }
}

/// The contexts a user binds keys in, outermost first, with their names.
/// "Everywhere" is the window's surfaces, not its dialogs.
pub const CONTEXTS: &[(&str, &str)] = &[
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): everywhere in the window (its surfaces, not dialogs)
    ("main", crate::catalogue::en::tr!("Everywhere")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the chats surface (chat, composer, explorer)
    ("workspace", crate::catalogue::en::tr!("Chats")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the chat and the explorer, not while typing
    ("navigation", crate::catalogue::en::tr!("Chat and explorer")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the chat pane
    ("pane", crate::catalogue::en::tr!("Chat")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the explorer, the list of chats
    ("sidebar", crate::catalogue::en::tr!("Explorer")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the message field
    ("composer", crate::catalogue::en::tr!("Composer")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the explorer's search field
    ("search", crate::catalogue::en::tr!("Explorer search")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the settings page
    ("settings", crate::catalogue::en::tr!("Settings")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the settings page's search field
    ("settings-search", crate::catalogue::en::tr!("Settings search")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the updates feed
    ("updates", crate::catalogue::en::tr!("Updates")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the teams page
    ("teams", crate::catalogue::en::tr!("Teams")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the hub that starts a new agent
    ("launch", crate::catalogue::en::tr!("New agent hub")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): an agent's profile
    ("profile", crate::catalogue::en::tr!("Agent profile")),
    // i18n: Key-binding editor and Keys help, the name of a key context (where a key works): the agent profile's backend picker
    ("transit", crate::catalogue::en::tr!("Backend picker")),
];

/// A context's name in the app's language.
pub fn context_name(state: &str) -> String {
    CONTEXTS.iter().find(|(s, _)| *s == state).map_or_else(|| state.to_owned(), |(_, name)| crate::catalogue::local(name))
}

/// Text fields, where a key without Ctrl or Alt types.
const TYPING: &[&str] = &["composer", "search", "settings-search"];

/// Keys added to and taken from one action's defaults in one context.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Change {
    pub add: Vec<String>,
    pub remove: Vec<String>,
}

/// action → context → change.
pub type Overrides = BTreeMap<String, BTreeMap<String, Change>>;

fn chain(state_name: &str) -> Vec<&'static str> {
    let mut chain = Vec::new();
    let mut current = STATES.iter().copied().find(|s| *s == state_name);
    while let Some(name) = current {
        chain.push(name);
        current = parent(name);
    }
    chain
}

/// The first definition of `action`, for its label and guard.
fn definition(action: &str) -> Option<Binding> {
    STATES.iter().flat_map(|name| state(name)).find(|b| b.action == action)
}

/// Every action a user may bind, with its label: not the keys a control
/// handles itself (the composer's Return) nor the link hints' and cards'
/// digits.
pub fn actions() -> Vec<(&'static str, String)> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for name in STATES {
        for entry in state(name) {
            let own = entry.native || entry.action.starts_with("hint-") || entry.action == "artifact-choose" || entry.action == "profile-section";
            if seen.insert(entry.action) && !own {
                out.push((entry.action, entry.label));
            }
        }
    }
    out
}

fn bindable(action: &str) -> bool {
    actions().iter().any(|(a, _)| *a == action)
}

fn is_typing(key: &str) -> bool {
    let first = key.split(' ').next().unwrap_or(key);
    let modified = first.starts_with("Ctrl+") || first.contains("Alt+");
    let base = first.rsplit_once('+').map_or(first, |(_, b)| if b.is_empty() { "+" } else { b });
    !modified && base != "Escape" && !(base.len() > 1 && base.starts_with('F'))
}

/// `defaults` with the user's changes along `chain`, outermost first; the
/// user's own keys lead.
fn with_changes(chain: &[&str], action: &str, defaults: Vec<String>, overrides: &Overrides) -> Vec<String> {
    let Some(contexts) = overrides.get(action) else { return defaults };
    let typing_here = chain.first().is_some_and(|s| TYPING.contains(s));
    let (mut keys, mut added) = (defaults, Vec::<String>::new());
    for (depth, context) in chain.iter().enumerate().rev() {
        let Some(change) = contexts.get(*context) else { continue };
        keys.retain(|k| !change.remove.contains(k));
        added.retain(|k| !change.remove.contains(k));
        for key in &change.add {
            if typing_here && depth > 0 && is_typing(key) {
                continue;
            }
            if !added.contains(key) {
                added.push(key.clone());
            }
        }
    }
    keys.retain(|k| !added.contains(k));
    added.extend(keys);
    added
}

/// Whether the user added `key` to `action` in a context on `chain`.
fn user_added(chain: &[&str], overrides: &Overrides, action: &str, key: &str) -> bool {
    overrides.get(action).is_some_and(|contexts| chain.iter().any(|c| contexts.get(*c).is_some_and(|ch| ch.add.iter().any(|k| k == key))))
}

/// A key the user gave one action is no other action's default there: a
/// default that came later (Ctrl+I for the profile) never takes a key the
/// user already uses.
fn yield_to_user(chain: &[&str], overrides: &Overrides, bindings: &mut [Binding]) {
    if overrides.is_empty() {
        return;
    }
    let taken: HashSet<String> = bindings.iter().flat_map(|b| b.keys.iter().filter(|k| user_added(chain, overrides, b.action, k)).cloned().collect::<Vec<_>>()).collect();
    if taken.is_empty() {
        return;
    }
    for binding in bindings.iter_mut() {
        let action = binding.action;
        binding.keys.retain(|k| !taken.contains(k) || user_added(chain, overrides, action, k));
    }
}

/// Whether one of `action`'s default keys went to another action the user
/// bound it to (`yield_to_user`).
pub fn yields(action: &str, overrides: &Overrides) -> bool {
    let defaults: Vec<String> = STATES.iter().flat_map(|name| state(name)).filter(|b| b.action == action).flat_map(|b| b.keys).collect();
    overrides.iter().any(|(other, contexts)| other != action && contexts.values().any(|ch| ch.add.iter().any(|k| defaults.contains(k))))
}

/// Bindings in effect in `state`, nearest state first; `facts` None ignores
/// the guards (for conflict checks). With facts (what runs and what the bar
/// shows), the user's own keys win over other actions' defaults.
pub fn resolve(state_name: &str, overrides: &Overrides, facts: Option<Facts>) -> Vec<Binding> {
    resolve_with(state_name, overrides, facts, facts.is_some())
}

fn resolve_with(state_name: &str, overrides: &Overrides, facts: Option<Facts>, yielding: bool) -> Vec<Binding> {
    let chain = chain(state_name);
    let (mut result, mut seen) = (Vec::new(), HashSet::new());
    for name in &chain {
        for mut entry in state(name) {
            if !seen.insert(entry.action) {
                continue;
            }
            entry.keys = with_changes(&chain, entry.action, std::mem::take(&mut entry.keys), overrides);
            result.push(entry);
        }
    }
    // Actions the user bound here that this state's map does not have.
    for (action, contexts) in overrides {
        if seen.contains(action.as_str()) || !chain.iter().any(|c| contexts.get(*c).is_some_and(|ch| !ch.add.is_empty())) {
            continue;
        }
        let Some(mut entry) = definition(action).filter(|d| bindable(d.action)) else { continue };
        entry.keys = with_changes(&chain, entry.action, Vec::new(), overrides);
        entry.hint = false;
        result.push(entry);
    }
    if yielding {
        yield_to_user(&chain, overrides, &mut result);
    }
    result.retain(|entry| !entry.keys.is_empty() && facts.is_none_or(|facts| facts.allows(entry.guard)));
    result
}

/// The action `chord` runs in `state`, if any. `native` bindings are left to
/// the focused control.
pub fn action_for(state_name: &str, chord: &str, overrides: &Overrides, facts: Facts) -> Option<&'static str> {
    resolve(state_name, overrides, Some(facts))
        .into_iter()
        .find(|entry| entry.keys.iter().any(|key| key == chord))
        .and_then(|entry| (!entry.native).then_some(entry.action))
}

/// How near `state` the binding of `key` to `action` is: 0 is the state
/// itself.
fn depth(chain: &[&str], action: &str, key: &str, overrides: &Overrides) -> usize {
    let added = chain.iter().position(|c| overrides.get(action).and_then(|a| a.get(*c)).is_some_and(|ch| ch.add.iter().any(|k| k == key)));
    let default = chain.iter().position(|c| state(c).iter().any(|b| b.action == action)).filter(|i| state(chain[*i]).iter().any(|b| b.action == action && b.keys.iter().any(|k| k == key)));
    added.into_iter().chain(default).min().unwrap_or(usize::MAX)
}

/// What the second press of `chord` in a row runs in `state`. The first
/// press has already done its own thing, at once, so a double press never
/// delays a key. Where that single press means something bound nearer
/// than the double (Right seeks a clip, unfolds a chat), each press keeps
/// that meaning: pressing it twice is doing it twice.
pub fn double_action(state_name: &str, chord: &str, overrides: &Overrides, facts: Facts) -> Option<&'static str> {
    let double = format!("{chord} {chord}");
    let resolved = resolve(state_name, overrides, Some(facts));
    let entry = resolved.iter().find(|e| !e.native && e.keys.contains(&double))?;
    let chain = chain(state_name);
    if let Some(single) = resolved.iter().find(|e| e.keys.iter().any(|k| k == chord)) {
        if depth(&chain, single.action, chord, overrides) < depth(&chain, entry.action, &double, overrides) {
            return None;
        }
    }
    Some(entry.action)
}

/// The keys `action` has in `state`, the user's first, whatever the guards.
pub fn keys_in(state_name: &str, action: &str, overrides: &Overrides) -> Vec<String> {
    resolve(state_name, overrides, None).into_iter().find(|b| b.action == action).map(|b| b.keys).unwrap_or_default()
}

/// The key to show for `action` in `state` (the user's first), spelled
/// for people.
pub fn shown(state_name: &str, action: &str, overrides: &Overrides) -> Option<String> {
    resolve_with(state_name, overrides, None, true).into_iter().find(|b| b.action == action).and_then(|b| b.keys.first().map(|k| display(k)))
}

/// `shown`, or `default` for an action the user never changed: what the
/// cards and their hints spell on every rebuild, without resolving the map.
pub fn shown_or(state_name: &str, action: &str, overrides: &Overrides, default: &str) -> String {
    if overrides.contains_key(action) || yields(action, overrides) { shown(state_name, action, overrides).unwrap_or_default() } else { default.to_owned() }
}

/// Whether the user bound `chord` themselves in `state` (or further out,
/// reaching it): their keys win over vim mode's.
pub fn user_bound(state_name: &str, chord: &str, overrides: &Overrides) -> bool {
    let chain = chain(state_name);
    resolve(state_name, overrides, None).iter().filter(|b| b.keys.iter().any(|k| k == chord)).any(|b| {
        overrides.get(b.action).is_some_and(|contexts| chain.iter().any(|c| contexts.get(*c).is_some_and(|ch| ch.add.iter().any(|k| k == chord))))
    })
}

/// The keys the user bound to `action` themselves that reach `state`,
/// double presses too.
pub fn user_keys(state_name: &str, action: &str, overrides: &Overrides) -> Vec<String> {
    let chain = chain(state_name);
    let Some(contexts) = overrides.get(action) else { return Vec::new() };
    keys_in(state_name, action, overrides).into_iter().filter(|k| chain.iter().any(|c| contexts.get(*c).is_some_and(|ch| ch.add.contains(k)))).collect()
}

/// The shortcut bar's hints for `state`.
pub fn hints(state_name: &str, overrides: &Overrides, facts: Facts) -> Vec<Binding> {
    resolve(state_name, overrides, Some(facts)).into_iter().filter(|entry| entry.hint && facts.allows(entry.hint_guard)).collect()
}

/// Remembers the last key, to tell a double press: the second press of the
/// same key within the window. A key held down repeating is not one.
#[derive(Debug, Default)]
pub struct Presses {
    last: Option<(String, Instant)>,
}

impl Presses {
    /// Notes a press; true when it is the second of a double press.
    pub fn press(&mut self, chord: &str, now: Instant, window: Duration, repeat: bool) -> bool {
        if repeat {
            self.last = None;
            return false;
        }
        let second = self.last.as_ref().is_some_and(|(key, at)| key == chord && now.saturating_duration_since(*at) <= window);
        self.last = if second { None } else { Some((chord.to_owned(), now)) };
        second
    }
}

/// The double press window, in milliseconds, from the setting.
pub fn double_press_window(setting: Option<i64>) -> Duration {
    Duration::from_millis(setting.unwrap_or(300).clamp(150, 1000) as u64)
}

const NAMED: &[&str] = &["Escape", "Tab", "Return", "Left", "Right", "Up", "Down", "Home", "End", "PageUp", "PageDown", "Delete", "Backspace", "Space"];

fn parse_chord(text: &str) -> Result<String, String> {
    // i18n: Key-binding editor and keymap import, error: the typed text is not a key; {} is that text
    let invalid = || tr!("Not a key: {}", text);
    let (mut control, mut alt, mut shift, mut rest) = (false, false, false, text);
    loop {
        let lower = rest.to_ascii_lowercase();
        let held = [("ctrl+", &mut control), ("alt+", &mut alt), ("shift+", &mut shift)].into_iter().find(|(p, _)| lower.starts_with(p) && rest.len() > p.len());
        let Some((prefix, flag)) = held else { break };
        *flag = true;
        rest = &rest[prefix.len()..];
    }
    let lower = rest.to_ascii_lowercase();
    let base = match lower.as_str() {
        "enter" => "Return".to_owned(),
        "esc" => "Escape".to_owned(),
        _ => match NAMED.iter().find(|n| n.to_ascii_lowercase() == lower) {
            Some(name) => (*name).to_owned(),
            None if lower.len() > 1 && lower.starts_with('f') && lower[1..].parse::<u8>().is_ok_and(|n| (1..=12).contains(&n)) => rest.to_ascii_uppercase(),
            None => {
                let mut chars = rest.chars();
                match (chars.next(), chars.next()) {
                    (Some(c), None) if !c.is_whitespace() && !c.is_control() => c.to_uppercase().to_string(),
                    _ => return Err(invalid()),
                }
            }
        },
    };
    let mut out = String::new();
    for (held, name) in [(control, "Ctrl+"), (alt, "Alt+"), (shift, "Shift+")] {
        if held {
            out += name;
        }
    }
    Ok(out + &base)
}

/// A key as the map spells it: a chord, or one chord twice for a double
/// press ("Right Right").
pub fn parse_key(text: &str) -> Result<String, String> {
    let parts: Vec<&str> = text.split_whitespace().collect();
    match parts.as_slice() {
        [one] => parse_chord(one),
        [first, second] => {
            let (first, second) = (parse_chord(first)?, parse_chord(second)?);
            if first != second {
                // i18n: Key-binding editor and keymap import, error: a double press must be the same key twice (e.g. Right Right); {} is what was typed
                return Err(tr!("A double press is one key twice: {}", text));
            }
            Ok(format!("{first} {second}"))
        }
        // i18n: Key-binding editor and keymap import, error: the typed text is not a key; {} is that text
        _ => Err(tr!("Not a key: {}", text)),
    }
}

/// A key as people read it: "Enter", "Esc", "Del", "Right ×2"; on macOS
/// Ctrl reads "Cmd", the key that runs it there (the Control key does too).
pub fn display(key: &str) -> String {
    let control = if cfg!(target_os = "macos") { "Cmd+" } else { "Ctrl+" };
    let one = |k: &str| k.replace("Ctrl+", control).replace("Return", "Enter").replace("Escape", "Esc").replace("Delete", "Del");
    match key.split_once(' ') {
        Some((first, _)) => format!("{} ×2", one(first)),
        None => one(key),
    }
}

fn reserved(key: &str) -> bool {
    key.split(' ').any(|k| RESERVED.contains(&k))
}

/// Two actions on one key in one state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Clash {
    pub state: &'static str,
    pub key: String,
    pub actions: [&'static str; 2],
}

/// Every clash in the map: two actions on one key where both would run.
/// A nearer native binding (the focused control's own key, such as the
/// composer's Ctrl+Shift+O) shadows an outer one on purpose.
pub fn clashes(overrides: &Overrides) -> Vec<Clash> {
    let mut found = Vec::new();
    for name in STATES {
        let mut seen: BTreeMap<String, (&'static str, bool)> = BTreeMap::new();
        for entry in resolve(name, overrides, None) {
            for key in &entry.keys {
                match seen.get(key) {
                    Some((_, true)) => continue,
                    Some((other, false)) if *other != entry.action => {
                        found.push(Clash { state: *name, key: key.clone(), actions: [*other, entry.action] });
                    }
                    _ => {
                        seen.insert(key.clone(), (entry.action, entry.native));
                    }
                }
            }
        }
    }
    found
}

/// Why a key cannot be added.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    Invalid(String),
    Reserved(String),
    /// Another action has it there; `take_over` moves it.
    Clash(Vec<Clash>),
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Refusal::Invalid(why) => f.write_str(why),
            // i18n: Key-binding editor, error: the key is kept for text editing (copy, paste, undo) and cannot be bound; {} is the key, e.g. Ctrl+C
            Refusal::Reserved(key) => f.write_str(&tr!("{} is reserved for text editing", display(key))),
            Refusal::Clash(clashes) => {
                let Some(first) = clashes.first() else { return Ok(()) };
                let other = first.actions[0];
                let label = definition(other).map_or_else(|| other.to_owned(), |d| d.label);
                let mut places: Vec<String> = Vec::new();
                for clash in clashes.iter().filter(|c| c.actions[0] == other) {
                    let place = context_name(clash.state);
                    if !places.contains(&place) {
                        places.push(place);
                    }
                }
                // i18n: Key-binding editor, the key is taken; {0} the key, {1} the action it runs now (a key action's name), {2} the key contexts, comma-separated
                f.write_str(&tr!("{0} already runs {1} in {2}", display(&first.key), label, places.join(", ")))
            }
        }
    }
}

fn change_mut<'a>(overrides: &'a mut Overrides, action: &str, context: &str) -> &'a mut Change {
    overrides.entry(action.to_owned()).or_default().entry(context.to_owned()).or_default()
}

fn tidy(mut overrides: Overrides) -> Overrides {
    for contexts in overrides.values_mut() {
        contexts.retain(|_, change| !change.add.is_empty() || !change.remove.is_empty());
    }
    overrides.retain(|_, contexts| !contexts.is_empty());
    overrides
}

fn checked_place(action: &str, context: &str) -> Result<(), Refusal> {
    if !bindable(action) {
        // i18n: Key-binding editor and keymap import, error: this action's keys cannot be changed; {} is the action's id (code)
        return Err(Refusal::Invalid(tr!("{} keeps its own keys", action)));
    }
    if !CONTEXTS.iter().any(|(c, _)| *c == context) {
        // i18n: Keymap import, error: the file names a key context that does not exist; {} is that context's id (code)
        return Err(Refusal::Invalid(tr!("No context {}", context)));
    }
    Ok(())
}

fn checked(action: &str, context: &str, key: &str) -> Result<String, Refusal> {
    checked_place(action, context)?;
    let key = parse_key(key).map_err(Refusal::Invalid)?;
    if reserved(&key) {
        return Err(Refusal::Reserved(key));
    }
    Ok(key)
}

/// Adds `key` in `context` itself: a key this context had taken away
/// comes back; any other is kept as the context's own, even one inherited
/// from further out (bound here on purpose, it outranks this context's
/// single presses and reaches its text field).
fn add_unchecked(overrides: &Overrides, action: &str, context: &str, key: &str) -> Overrides {
    let mut next = overrides.clone();
    let change = change_mut(&mut next, action, context);
    let restored = change.remove.iter().any(|k| k == key);
    change.remove.retain(|k| k != key);
    let back = restored && keys_in(context, action, &next).iter().any(|k| k == key);
    let change = change_mut(&mut next, action, context);
    if !back && !change.add.iter().any(|k| k == key) {
        change.add.push(key.to_owned());
    }
    tidy(next)
}

/// Adds `key` to `action` in `context` (and the contexts under it).
pub fn add(overrides: &Overrides, action: &str, context: &str, key: &str) -> Result<Overrides, Refusal> {
    let key = checked(action, context, key)?;
    let next = add_unchecked(overrides, action, context, &key);
    let new: Vec<Clash> = clashes(&next).into_iter().filter(|c| c.key == key && c.actions.contains(&action)).collect();
    if new.is_empty() { Ok(next) } else { Err(Refusal::Clash(new)) }
}

/// Adds `key` and takes it from the actions it clashed with, where they
/// clashed.
pub fn take_over(overrides: &Overrides, action: &str, context: &str, key: &str) -> Result<Overrides, String> {
    let key = checked(action, context, key).map_err(|r| r.to_string())?;
    let mut next = add_unchecked(overrides, action, context, &key);
    for _ in 0..64 {
        let Some(clash) = clashes(&next).into_iter().find(|c| c.key == key && c.actions.contains(&action)) else { return Ok(next) };
        let other = if clash.actions[0] == action { clash.actions[1] } else { clash.actions[0] };
        next = remove(&next, other, clash.state, &key);
    }
    // i18n: Key-binding editor, error: the key could not be freed from the other actions; {} is the key
    Err(tr!("{} still clashes", display(&key)))
}

/// Takes `key` from `action` in `context`.
pub fn remove(overrides: &Overrides, action: &str, context: &str, key: &str) -> Overrides {
    let mut next = overrides.clone();
    change_mut(&mut next, action, context).add.retain(|k| k != key);
    if keys_in(context, action, &next).iter().any(|k| k == key) && !next[action][context].remove.iter().any(|k| k == key) {
        change_mut(&mut next, action, context).remove.push(key.to_owned());
    }
    tidy(next)
}

/// Back to the defaults for `action` in `context`.
pub fn reset(overrides: &Overrides, action: &str, context: &str) -> Overrides {
    let mut next = overrides.clone();
    if let Some(contexts) = next.get_mut(action) {
        contexts.remove(context);
    }
    tidy(next)
}

/// Whether the user changed `action` in `context`.
pub fn customised(overrides: &Overrides, action: &str, context: &str) -> bool {
    overrides.get(action).is_some_and(|c| c.contains_key(context))
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(str::to_owned).collect()).unwrap_or_default()
}

/// An action's old single key: it replaced the default wherever the
/// action had one.
fn legacy(action: &str, key: &str) -> BTreeMap<String, Change> {
    STATES
        .iter()
        .filter_map(|name| state(name).into_iter().find(|b| b.action == action).map(|b| (name, b.keys)))
        .map(|(name, keys)| ((*name).to_owned(), Change { add: vec![key.to_owned()], remove: keys }))
        .collect()
}

/// The setting as saved, the one-chord form from before included; what it
/// cannot read is left out.
pub fn from_settings(value: &Value) -> Overrides {
    let mut overrides = Overrides::new();
    for (action, contexts) in value.as_object().into_iter().flatten() {
        let record = match contexts {
            Value::String(key) => legacy(action, key),
            Value::Object(contexts) => contexts
                .iter()
                .map(|(context, change)| (context.clone(), Change { add: strings(change.get("add")), remove: strings(change.get("remove")) }))
                .collect(),
            _ => continue,
        };
        overrides.insert(action.clone(), record);
    }
    tidy(overrides)
}

pub fn to_settings(overrides: &Overrides) -> Value {
    let record = |change: &Change| {
        let mut out = Map::new();
        if !change.add.is_empty() {
            out.insert("add".into(), json!(change.add));
        }
        if !change.remove.is_empty() {
            out.insert("remove".into(), json!(change.remove));
        }
        Value::Object(out)
    };
    Value::Object(overrides.iter().map(|(action, contexts)| (action.clone(), Value::Object(contexts.iter().map(|(c, ch)| (c.clone(), record(ch))).collect()))).collect())
}

/// Validates an exported keymap (`{"version": 2, "bindings": {...}}`, or
/// the one-chord version 1) and returns its bindings: known actions and
/// contexts, real keys, no reserved editing key, and no clash.
pub fn import(text: &str) -> Result<Overrides, String> {
    let value: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    // i18n: Keymap import, error: the file is not a keymap Clarp can read
    let object = value.as_object().ok_or_else(|| tr!("Unsupported keymap"))?;
    let supported = object.keys().all(|k| k == "version" || k == "bindings");
    let bindings = object.get("bindings").and_then(Value::as_object);
    let version = object.get("version").and_then(Value::as_i64);
    let (true, Some(version @ (1 | 2)), Some(bindings)) = (supported, version, bindings) else {
        // i18n: Keymap import, error: the file is not a keymap Clarp can read
        return Err(tr!("Unsupported keymap"));
    };
    for (action, contexts) in bindings {
        let readable = match (version, contexts) {
            (1, Value::String(_)) => true,
            (2, Value::Object(contexts)) => contexts.values().all(|c| c.as_object().is_some_and(|c| c.keys().all(|k| k == "add" || k == "remove") && c.values().all(|v| v.as_array().is_some_and(|a| a.iter().all(Value::is_string))))),
            _ => false,
        };
        if !readable {
            // i18n: Keymap import, error: one action's keys in the file cannot be read; {} is the action's id (code)
            return Err(tr!("Unsupported binding for {}", action));
        }
    }
    let overrides = from_settings(&Value::Object(bindings.clone()));
    let mut canonical = Overrides::new();
    for (action, contexts) in &overrides {
        for (context, change) in contexts {
            let mut keys = Change::default();
            for key in &change.add {
                keys.add.push(checked(action, context, key).map_err(|r| r.to_string())?);
            }
            // A default may be taken away, reserved or not (Ctrl+C stops an agent).
            for key in &change.remove {
                checked_place(action, context).map_err(|r| r.to_string())?;
                keys.remove.push(parse_key(key)?);
            }
            canonical.entry(action.clone()).or_default().insert(context.clone(), keys);
        }
    }
    // The user's key against another action's default is no conflict: the
    // user's wins (`yield_to_user`), as it does for keys kept in settings.
    let mine = |clash: &Clash| clash.actions.iter().filter(|a| user_added(&chain(clash.state), &canonical, a, &clash.key)).count();
    if let Some(clash) = clashes(&canonical).iter().find(|c| mine(c) != 1) {
        // i18n: Keymap import, error: two actions share one key; {0} the key context's name, {1} the key
        return Err(tr!("Conflict in {0}: {1}", context_name(clash.state), display(&clash.key)));
    }
    Ok(canonical)
}

pub fn export(overrides: &Overrides) -> String {
    serde_json::to_string_pretty(&json!({"version": 2, "bindings": to_settings(overrides)})).unwrap_or_default()
}

/// A key event as the map spells it ("Ctrl+Alt+V", "Shift+Tab", "Escape",
/// "E"), or None for a bare modifier.
pub fn chord(text: &str, control: bool, alt: bool, shift: bool) -> Option<String> {
    use slint::platform::Key;
    let named = [
        (Key::Escape, "Escape"),
        (Key::Tab, "Tab"),
        (Key::Backtab, "Tab"),
        (Key::Return, "Return"),
        (Key::LeftArrow, "Left"),
        (Key::RightArrow, "Right"),
        (Key::UpArrow, "Up"),
        (Key::DownArrow, "Down"),
        (Key::Home, "Home"),
        (Key::End, "End"),
        (Key::PageUp, "PageUp"),
        (Key::PageDown, "PageDown"),
        (Key::Delete, "Delete"),
        (Key::Backspace, "Backspace"),
        (Key::F1, "F1"),
        (Key::F2, "F2"),
        (Key::F3, "F3"),
        (Key::F4, "F4"),
        (Key::F5, "F5"),
        (Key::F6, "F6"),
        (Key::F7, "F7"),
        (Key::F8, "F8"),
        (Key::F9, "F9"),
        (Key::F10, "F10"),
        (Key::F11, "F11"),
        (Key::F12, "F12"),
        (Key::Space, "Space"),
    ];
    let modifiers = [Key::Control, Key::Alt, Key::AltGr, Key::Shift, Key::ShiftR, Key::Meta, Key::MetaR, Key::ControlR];
    if modifiers.iter().any(|m| slint::SharedString::from(*m) == text) {
        return None;
    }
    let backtab = slint::SharedString::from(Key::Backtab) == text;
    let key = match named.iter().find(|(k, _)| slint::SharedString::from(*k) == text) {
        Some((_, name)) => (*name).to_owned(),
        None if text == " " => "Space".to_owned(),
        None => {
            let mut chars = text.chars();
            let (Some(c), None) = (chars.next(), chars.next()) else { return None };
            if c.is_control() {
                return None;
            }
            c.to_uppercase().to_string()
        }
    };
    // A letter's case already says Shift; symbols keep what the layout made.
    let letter = key.len() == 1 && key.chars().all(|c| c.is_ascii_alphabetic());
    let shift = (shift || backtab) && (letter || key.len() > 1);
    let mut out = String::new();
    for (held, name) in [(control, "Ctrl+"), (alt, "Alt+"), (shift, "Shift+")] {
        if held {
            out += name;
        }
    }
    Some(out + &key)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all() -> Facts {
        Facts { attention: true, agent: true, rows: true, can_send: true, busy: true, playing: true, behind: true, folds: true, artifacts: true, artifact: true, layout_warning: true, work: true, queued: true }
    }

    #[test]
    fn child_states_inherit_and_override_by_action() {
        let none = Overrides::new();
        // Escape in a pane is navigation's "Chat", not workspace's "Navigate".
        let pane = resolve("pane", &none, Some(all()));
        assert_eq!(pane.iter().find(|b| b.action == "escape").unwrap().label, "Chat");
        assert_eq!(pane.iter().find(|b| b.action == "split-right").unwrap().keys, ["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"]);
        assert!(pane.iter().any(|b| b.action == "settings"), "main's bindings reach a pane");
        assert_eq!(action_for("pane", "E", &none, all()), Some("focus-sidebar"));
        // Ctrl+= / Ctrl+- / Ctrl+0 zoom the chat, from the chat and the
        // composer alike; the window's scale is Ctrl+Alt+PageUp/PageDown/0.
        for state in ["pane", "composer", "sidebar"] {
            assert_eq!(action_for(state, "Ctrl+=", &none, all()), Some("chat-zoom-in"), "{state}");
            assert_eq!(action_for(state, "Ctrl++", &none, all()), Some("chat-zoom-in"), "{state}: = needs Shift on some layouts");
            assert_eq!(action_for(state, "Ctrl+-", &none, all()), Some("chat-zoom-out"), "{state}");
            assert_eq!(action_for(state, "Ctrl+0", &none, all()), Some("chat-zoom-reset"), "{state}");
            assert_eq!(action_for(state, "Ctrl+Alt+PageUp", &none, all()), Some("ui-larger"), "{state}");
            assert_eq!(action_for(state, "Ctrl+Alt+PageDown", &none, all()), Some("ui-smaller"), "{state}");
            assert_eq!(action_for(state, "Ctrl+Alt+0", &none, all()), Some("ui-reset"), "{state}");
        }
        assert_eq!(action_for("sidebar", "Left", &none, all()), Some("fold"));
        assert_eq!(action_for("sidebar", "H", &none, all()), Some("focus-pane"), "H is the chat everywhere");
        assert_eq!(action_for("sidebar", "P", &none, all()), Some("toggle-preview"));
        assert_eq!(action_for("composer", "Ctrl+E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("composer", "Ctrl+H", &none, all()), Some("focus-pane"));
        assert_eq!(action_for("pane", "Ctrl+E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("sidebar", "Right", &none, all()), Some("unfold"));
        assert_eq!(action_for("composer", "E", &none, all()), None, "letters type in the composer");
        assert_eq!(action_for("composer", "Return", &none, all()), None, "the composer sends by itself");
        assert_eq!(action_for("composer", "Ctrl+Alt+V", &none, all()), Some("split-right"));
        assert!(resolve("blocked", &none, Some(all())).is_empty());
        assert_eq!(action_for("pane", "F", &none, all()), Some("link-hints"));
        assert_eq!(action_for("composer", "Ctrl+L", &none, all()), Some("link-hints"), "the chord works while typing");
        assert_eq!(action_for("composer", "F", &none, all()), None, "F types in the composer");
        assert_eq!(action_for("hints", "7", &none, all()), Some("hint-digit"));
        assert_eq!(action_for("hints", "Escape", &none, all()), Some("hint-cancel"));
        assert_eq!(action_for("hints", "Ctrl+K", &none, all()), None, "hints own the keyboard");
    }

    #[test]
    fn processes_open_from_the_chat_the_composer_the_explorer_and_updates() {
        let none = Overrides::new();
        for state in ["pane", "composer", "sidebar", "search"] {
            assert_eq!(action_for(state, "Ctrl+Shift+P", &none, all()), Some("agent-processes"), "{state}");
        }
        assert_eq!(action_for("sidebar", "Shift+P", &none, all()), Some("agent-processes"), "the explorer row's own key");
        assert_eq!(action_for("sidebar", "P", &none, all()), Some("toggle-preview"), "P stays the preview");
        assert_eq!(action_for("composer", "Shift+P", &none, all()), None, "P types in the composer");
        assert_eq!(action_for("updates", "Ctrl+Shift+P", &none, Facts::default()), Some("agent-processes"), "Updates lists every job");
        assert_eq!(action_for("pane", "Ctrl+Shift+P", &none, Facts::default()), None, "no agent, nothing to list");
        // The bar offers it while something runs; the key works regardless.
        let idle = Facts { agent: true, ..Facts::default() };
        assert_eq!(action_for("composer", "Ctrl+Shift+P", &none, idle), Some("agent-processes"));
        assert!(!hints("composer", &none, idle).iter().any(|b| b.action == "agent-processes"));
        assert!(hints("composer", &none, Facts { work: true, ..idle }).iter().any(|b| b.action == "agent-processes"));
        assert!(hints("updates", &none, Facts::default()).iter().any(|b| b.action == "agent-processes" && b.label == "Jobs"));
        // Customisable like the rest: a key of one's own in the chats.
        assert!(actions().iter().any(|(a, l)| *a == "agent-processes" && *l == "Processes"));
        let mut mine = Overrides::new();
        mine.entry("agent-processes".into()).or_default().insert("workspace".into(), Change { add: vec!["Ctrl+Alt+J".into()], remove: vec!["Ctrl+Shift+P".into()] });
        assert_eq!(action_for("composer", "Ctrl+Alt+J", &mine, all()), Some("agent-processes"));
        assert_eq!(action_for("composer", "Ctrl+Shift+P", &mine, all()), None, "the default key is taken off");
        // No other binding answers to it anywhere.
        for state in STATES {
            for binding in resolve(state, &none, Some(all())) {
                if binding.action != "agent-processes" {
                    assert!(!binding.keys.iter().any(|k| k == "Ctrl+Shift+P"), "{state}: {} also has Ctrl+Shift+P", binding.action);
                }
            }
        }
    }

    #[test]
    fn guards_hide_bindings_that_cannot_run() {
        let none = Overrides::new();
        let nothing = Facts::default();
        assert_eq!(action_for("pane", "N", &none, nothing), None);
        assert_eq!(action_for("pane", "N", &none, Facts { attention: true, ..nothing }), Some("next-attention"));
        assert_eq!(action_for("pane", "Ctrl+End", &none, nothing), None);
        assert!(!hints("composer", &none, nothing).iter().any(|b| b.action == "queue"));
        assert!(hints("composer", &none, all()).iter().any(|b| b.action == "queue"));
    }

    /// With the live status line hidden, the bar names the stop key while
    /// the agent works, wherever the keyboard is; idle, the key still works
    /// but the bar does not offer it.
    #[test]
    fn the_bar_names_stop_while_the_agent_works() {
        let none = Overrides::new();
        let idle = Facts { agent: true, ..Facts::default() };
        let busy = Facts { busy: true, ..idle };
        for state in ["pane", "composer", "sidebar"] {
            let stop = hints(state, &none, busy).into_iter().find(|b| b.action == "stop-agent");
            assert!(stop.as_ref().is_some_and(|b| b.keys.first().map(String::as_str) == Some("Ctrl+.") && b.label == "Stop"), "{state}: {stop:?}");
            assert!(!hints(state, &none, idle).iter().any(|b| b.action == "stop-agent"), "{state}: not while idle");
        }
        assert_eq!(action_for("pane", "Ctrl+.", &none, idle), Some("stop-agent"), "the key works idle as before");
    }

    fn peter() -> Overrides {
        // Ctrl+J is next attention's: taking it over leaves N for that.
        let clash = add(&Overrides::new(), "next-agent", "main", "Ctrl+J").unwrap_err();
        let Refusal::Clash(found) = clash.clone() else { panic!("Ctrl+J is next attention's: {clash:?}") };
        assert!(found.iter().any(|c| c.actions.contains(&"next-attention") && c.state == "main"), "{found:?}");
        let taken = take_over(&Overrides::new(), "next-agent", "main", "Ctrl+J").unwrap();
        add(&taken, "next-agent", "main", "Right Right").unwrap()
    }

    #[test]
    fn keys_parse_and_spell_doubles() {
        assert_eq!(parse_key("ctrl+j").as_deref(), Ok("Ctrl+J"));
        assert_eq!(parse_key("Shift+Ctrl+alt+v").as_deref(), Ok("Ctrl+Alt+Shift+V"), "modifiers in the map's order");
        assert_eq!(parse_key("Right Right").as_deref(), Ok("Right Right"));
        assert_eq!(parse_key(" right   right ").as_deref(), Ok("Right Right"));
        assert_eq!(parse_key("Enter").as_deref(), Ok("Return"));
        assert_eq!(parse_key("Ctrl+=").as_deref(), Ok("Ctrl+="));
        assert_eq!(parse_key("F7").as_deref(), Ok("F7"));
        assert!(parse_key("Right Left").is_err(), "a double press is one key twice");
        assert!(parse_key("J J J").is_err());
        assert!(parse_key("Ctrl+").is_err());
        assert!(parse_key("Ctrl+Banana").is_err());
        assert!(parse_key("").is_err());
        assert_eq!(display("Right Right"), "Right ×2");
        assert_eq!(display("Ctrl+Return"), if cfg!(target_os = "macos") { "Cmd+Enter" } else { "Ctrl+Enter" });
        assert_eq!(display("Escape"), "Esc");
    }

    #[test]
    fn several_bindings_per_action_save_and_load() {
        let overrides = peter();
        assert_eq!(keys_in("pane", "next-agent", &overrides), ["Ctrl+J", "Right Right"]);
        assert_eq!(from_settings(&to_settings(&overrides)), overrides, "kept in settings as is");
        assert_eq!(import(&export(&overrides)).unwrap(), overrides, "and in an exported profile");
        assert!(export(&overrides).contains("\"version\": 2"));
        // A one-chord profile from before still loads: its key replaces the default.
        let old = import(r#"{"version": 1, "bindings": {"switcher": "Ctrl+P"}}"#).unwrap();
        assert_eq!(action_for("pane", "Ctrl+P", &old, all()), Some("switcher"));
        assert_eq!(action_for("pane", "Ctrl+K", &old, all()), None, "the default key is replaced");
        assert_eq!(action_for("launch", "Ctrl+P", &old, all()), Some("switcher"));
        assert_eq!(from_settings(&json!({"switcher": "Ctrl+P"})), old, "the old setting too");
        assert!(import(r#"{"version": 3, "bindings": {}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"no-such-action": {"main": {"add": ["Ctrl+Q"]}}}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"mute": {"nowhere": {"add": ["Ctrl+Q"]}}}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"mute": {"main": {"add": ["Right Left"]}}}}"#).is_err());
    }

    #[test]
    fn user_bindings_reach_child_contexts_and_add_to_defaults() {
        let overrides = peter();
        for state in ["main", "workspace", "navigation", "pane", "sidebar", "settings", "updates", "composer"] {
            assert_eq!(action_for(state, "Ctrl+J", &overrides, all()), Some("next-agent"), "Ctrl+J in {state}");
        }
        assert_eq!(action_for("pane", "N", &overrides, all()), Some("next-attention"), "N is still next attention");
        assert_eq!(action_for("launch", "Ctrl+J", &overrides, all()), None, "dialogs are not everywhere");
        // A binding in the explorer is the explorer's alone.
        let explorer = add(&Overrides::new(), "mute", "sidebar", "Shift+M").unwrap();
        assert_eq!(action_for("sidebar", "Shift+M", &explorer, all()), Some("mute"));
        assert_eq!(action_for("pane", "Shift+M", &explorer, all()), None);
        // Any action, not only a few; removing a default and resetting it.
        let gone = remove(&Overrides::new(), "next-attention", "navigation", "N");
        assert_eq!(action_for("sidebar", "N", &gone, all()), None);
        assert_eq!(action_for("sidebar", "Ctrl+J", &gone, all()), Some("next-attention"));
        assert!(reset(&gone, "next-attention", "navigation").is_empty());
        let both = remove(&peter(), "next-agent", "main", "Ctrl+J");
        assert_eq!(keys_in("pane", "next-agent", &both), ["Right Right"]);
        // The user's own keys come first, for the bar.
        let extra = add(&Overrides::new(), "switcher", "main", "Ctrl+P").unwrap();
        assert_eq!(keys_in("pane", "switcher", &extra), ["Ctrl+P", "Space", "Ctrl+K"]);
        assert_eq!(hints("pane", &extra, all()).iter().find(|b| b.action == "switcher").unwrap().keys[0], "Ctrl+P");
    }

    #[test]
    fn typing_keys_from_outside_never_reach_the_composer() {
        let overrides = peter();
        assert!(!keys_in("composer", "next-agent", &overrides).contains(&"Right Right".to_owned()));
        assert_eq!(double_action("composer", "Right", &overrides, all()), None, "Right Right moves the cursor twice");
        let letters = add(&Overrides::new(), "mute", "main", "M").unwrap();
        assert_eq!(action_for("pane", "M", &letters, all()), Some("mute"));
        assert_eq!(action_for("composer", "M", &letters, all()), None, "M types");
        assert_eq!(action_for("search", "M", &letters, all()), None);
        // Bound there on purpose, it works there.
        let there = add(&overrides, "next-agent", "composer", "Right Right").unwrap();
        assert_eq!(double_action("composer", "Right", &there, all()), Some("next-agent"));
    }

    #[test]
    fn clashes_are_found_refused_and_taken_over() {
        let clash = add(&Overrides::new(), "next-agent", "main", "J").unwrap_err();
        let Refusal::Clash(found) = clash.clone() else { panic!("{clash:?}") };
        let states: Vec<&str> = found.iter().map(|c| c.state).collect();
        assert!(states.contains(&"sidebar") && states.contains(&"pane"), "J is the explorer's next and the chat's next card: {states:?}");
        let taken = take_over(&Overrides::new(), "next-agent", "main", "J").unwrap();
        assert!(clashes(&taken).is_empty());
        assert_eq!(action_for("sidebar", "J", &taken, all()), Some("next-agent"));
        assert_eq!(action_for("sidebar", "Down", &taken, all()), Some("agent-next"), "the other keys stay");
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Ctrl+V"), Err(Refusal::Reserved(_))));
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Ctrl+C Ctrl+C"), Err(Refusal::Reserved(_))));
        assert!(take_over(&Overrides::new(), "zoom", "main", "Ctrl+Z").is_err(), "reserved cannot be taken over");
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Banana"), Err(Refusal::Invalid(_))));
        // A double press is not its single press.
        assert!(add(&Overrides::new(), "next-agent", "sidebar", "Right Right").is_ok());
        assert!(clashes(&Overrides::new()).is_empty(), "the defaults agree");
        let clash = add(&peter(), "mute", "pane", "Right Right").unwrap_err();
        assert!(matches!(clash, Refusal::Clash(_)), "inherited from everywhere: {clash:?}");
        assert!(import(&export(&add_unchecked(&peter(), "mute", "pane", "Right Right"))).unwrap_err().contains("Right ×2"), "a clashing profile is refused");
    }

    #[test]
    fn a_double_press_is_its_second_press_within_the_window() {
        let window = std::time::Duration::from_millis(300);
        let t0 = std::time::Instant::now();
        let ms = |n| t0 + std::time::Duration::from_millis(n);
        let mut presses = Presses::default();
        assert!(!presses.press("Right", t0, window, false));
        assert!(presses.press("Right", ms(200), window, false), "the second in time");
        assert!(!presses.press("Right", ms(300), window, false), "a third starts again");
        assert!(!presses.press("Right", ms(700), window, false), "too late");
        assert!(!presses.press("Left", ms(750), window, false));
        assert!(!presses.press("Right", ms(800), window, false), "another key between");
        assert!(!presses.press("Right", ms(820), window, true), "a held key repeating is not a double press");
        assert!(!presses.press("Right", ms(900), window, false));
        assert!(presses.press("Right", ms(1199), window, false));
    }

    #[test]
    fn a_single_press_keeps_its_meaning() {
        let overrides = peter();
        let nothing = Facts { rows: true, ..Facts::default() };
        // Nothing is on Right in the chat: the second Right is the next agent.
        assert_eq!(action_for("pane", "Right", &overrides, nothing), None);
        assert_eq!(double_action("pane", "Right", &overrides, nothing), Some("next-agent"));
        // On a gallery or a clip Right steps or seeks, each press, at once.
        let card = Facts { artifact: true, artifacts: true, ..nothing };
        assert_eq!(action_for("pane", "Right", &overrides, card), Some("artifact-forward"));
        assert_eq!(double_action("pane", "Right", &overrides, card), None, "seeking twice is not a jump");
        // The explorer's Right unfolds; with nothing to unfold it is free.
        assert_eq!(double_action("sidebar", "Right", &overrides, Facts { folds: true, ..nothing }), None);
        assert_eq!(double_action("sidebar", "Right", &overrides, nothing), Some("next-agent"));
        // Bound in the explorer itself, the first Right unfolds and the second jumps.
        let there = add(&overrides, "next-agent", "sidebar", "Right Right").unwrap();
        assert_eq!(action_for("sidebar", "Right", &there, Facts { folds: true, ..nothing }), Some("unfold"));
        assert_eq!(double_action("sidebar", "Right", &there, Facts { folds: true, ..nothing }), Some("next-agent"));
        assert_eq!(double_action("pane", "Left", &overrides, nothing), None);
    }

    #[test]
    fn every_action_can_be_bound_but_not_a_control_s_own_keys() {
        let actions: Vec<&str> = actions().iter().map(|(a, _)| *a).collect();
        for action in ["next-agent", "previous-agent", "mute", "next-attention", "agent-next", "orchestrator", "escape"] {
            assert!(actions.contains(&action), "{action}");
        }
        for action in ["send", "newline", "hint-digit", "artifact-choose", "launch-model"] {
            assert!(!actions.contains(&action), "{action} is the control's");
        }
        assert!(matches!(add(&Overrides::new(), "send", "composer", "Ctrl+Q"), Err(Refusal::Invalid(_))));
    }

    #[test]
    fn tabs_have_ctrl_keys_and_the_hub_keeps_ctrl_t() {
        let none = Overrides::new();
        for state in ["pane", "sidebar", "composer", "settings"] {
            assert_eq!(action_for(state, "Ctrl+T", &none, all()), Some("new-workspace"), "Ctrl+T in {state}");
            assert_eq!(action_for(state, "Ctrl+Tab", &none, all()), Some("next-workspace"), "Ctrl+Tab in {state}");
            assert_eq!(action_for(state, "Ctrl+Shift+Tab", &none, all()), Some("previous-workspace"), "Ctrl+Shift+Tab in {state}");
        }
        assert_eq!(action_for("pane", "Ctrl+Alt+W", &none, all()), Some("next-workspace"), "the old key stays");
        // Ctrl+W closes a tab from the chat and the explorer, never while typing.
        assert_eq!(action_for("pane", "Ctrl+W", &none, all()), Some("close-workspace"));
        assert_eq!(action_for("sidebar", "Ctrl+W", &none, all()), Some("close-workspace"));
        assert_eq!(action_for("composer", "Ctrl+W", &none, all()), None);
        assert_eq!(action_for("pane", "Ctrl+Shift+W", &none, all()), Some("close-pane"), "Ctrl+Shift+W still closes a pane");
        // In the New Session hub Ctrl+T is still Show all.
        assert_eq!(keys_in("launch", "launch-show-all", &none), ["Ctrl+T"]);
        assert_eq!(action_for("launch", "Ctrl+T", &none, all()), None, "the hub handles it");
        // Capital J and K walk the cards too (vim mode's way onto them).
        assert_eq!(action_for("pane", "Shift+K", &none, all()), Some("artifact-previous"));
        assert_eq!(action_for("pane", "Shift+J", &none, all()), Some("artifact-next"));
        assert!(clashes(&none).is_empty(), "the defaults agree: {:?}", clashes(&none));
    }

    #[test]
    fn question_mark_is_help_outside_text_fields() {
        let none = Overrides::new();
        for state in ["pane", "sidebar", "settings", "updates"] {
            assert_eq!(action_for(state, "?", &none, all()), Some("help-keys"), "? in {state}");
        }
        assert_eq!(action_for("composer", "?", &none, all()), None, "? types in the composer");
        assert_eq!(action_for("search", "?", &none, all()), None);
        assert_eq!(user_keys("pane", "next-agent", &peter()), ["Ctrl+J", "Right Right"]);
        assert!(user_keys("pane", "switcher", &peter()).is_empty(), "defaults are not the user's");
    }

    #[test]
    fn the_user_s_own_keys_are_known_so_they_win_over_vim() {
        let mine = add(&Overrides::new(), "mute", "main", "M").unwrap();
        assert!(user_bound("pane", "M", &mine));
        assert!(user_bound("sidebar", "M", &mine));
        assert!(!user_bound("composer", "M", &mine), "a typing key bound further out never reaches the composer");
        assert!(!user_bound("pane", "J", &mine), "a default is not the user's");
        let removed = remove(&Overrides::new(), "next-attention", "navigation", "N");
        assert!(!user_bound("pane", "N", &removed), "taking a key away binds nothing");
    }

    #[test]
    fn key_events_are_spelled_like_the_map() {
        use slint::platform::Key;
        let key = |k: Key| slint::SharedString::from(k).to_string();
        assert_eq!(chord("v", true, true, false).as_deref(), Some("Ctrl+Alt+V"));
        assert_eq!(chord("e", false, false, false).as_deref(), Some("E"));
        assert_eq!(chord(&key(Key::Escape), false, false, false).as_deref(), Some("Escape"));
        assert_eq!(chord(&key(Key::Backtab), false, false, true).as_deref(), Some("Shift+Tab"));
        assert_eq!(chord(&key(Key::Return), true, false, false).as_deref(), Some("Ctrl+Return"));
        assert_eq!(chord(" ", false, false, false).as_deref(), Some("Space"));
        assert_eq!(chord("=", true, false, true).as_deref(), Some("Ctrl+="), "a symbol keeps its own shift");
        assert_eq!(chord(&key(Key::Control), true, false, false), None);
    }

    #[test]
    fn v_in_the_chat_selects_text() {
        let none = Overrides::new();
        assert_eq!(action_for("pane", "V", &none, all()), Some("select-text"));
        assert_eq!(action_for("sidebar", "V", &none, all()), Some("toggle-compact"), "V in the explorer stays the compact view");
    }

    #[test]
    fn w_shows_work_and_c_the_other_view_in_the_chat() {
        let none = Overrides::new();
        assert_eq!(action_for("pane", "W", &none, all()), Some("show-work"));
        assert_eq!(action_for("pane", "C", &none, all()), Some("chat-view"));
        assert_eq!(action_for("composer", "W", &none, all()), None, "never while typing");
        assert_eq!(action_for("sidebar", "C", &none, all()), None, "nor in the explorer");
    }

    #[test]
    fn ctrl_f_searches_messages_from_anywhere_in_the_chats() {
        let none = Overrides::new();
        for state in ["composer", "pane", "sidebar"] {
            assert_eq!(action_for(state, "Ctrl+F", &none, all()), Some("search-messages"), "{state}");
        }
        assert_eq!(action_for("sidebar", "/", &none, all()), Some("agent-search"), "/ still filters the explorer");
    }

    #[test]
    fn shift_f5_refreshes_the_agents_and_f5_the_chat() {
        let none = Overrides::new();
        let f5 = slint::SharedString::from(slint::platform::Key::F5).to_string();
        assert_eq!(chord(&f5, false, false, true).as_deref(), Some("Shift+F5"));
        for state in ["pane", "sidebar"] {
            assert_eq!(action_for(state, "Shift+F5", &none, all()), Some("refresh-agents"), "{state}");
            assert_eq!(action_for(state, "F5", &none, all()), Some("refresh"), "{state}");
        }
    }

    #[test]
    fn option_keeps_making_characters_while_typing() {
        // On a Mac Option+letter types a character (Option+E an accent):
        // nothing bound where text is typed may take Alt with one key alone.
        for typing in ["composer", "search", "settings-search"] {
            let mut name = Some(typing);
            while let Some(current) = name {
                for binding in state(current) {
                    for key in &binding.keys {
                        let plain_alt = key.strip_prefix("Alt+").is_some_and(|rest| rest.chars().count() == 1);
                        assert!(!plain_alt, "{typing} reaches {current}'s {key} ({})", binding.action);
                    }
                }
                name = parent(current);
            }
        }
    }

    /// Ctrl+I opens the agent profile wherever an agent is open, the
    /// composer included; inside the profile digits, arrows, Tab and Enter
    /// are its own, Escape closes it. A user who already had Ctrl+I keeps it.
    #[test]
    fn ctrl_i_opens_the_profile_and_the_profile_has_its_keys() {
        let none = Overrides::new();
        for state in ["pane", "composer", "sidebar", "search", "settings", "updates"] {
            assert_eq!(action_for(state, "Ctrl+I", &none, all()), Some("agent-profile"), "{state}");
        }
        assert_eq!(action_for("pane", "Ctrl+I", &none, Facts::default()), None, "no agent, no profile");
        assert!(hints("composer", &none, all()).iter().any(|b| b.action == "agent-profile" && b.keys[0] == "Ctrl+I"));
        for state in ["launch", "modal", "transit", "hints"] {
            assert_ne!(action_for(state, "Ctrl+I", &none, all()), Some("agent-profile"), "{state}: dialogs keep their keys");
        }
        assert_eq!(action_for("profile", "3", &none, all()), Some("profile-section"));
        assert_eq!(action_for("profile", "Down", &none, all()), Some("profile-down"));
        assert_eq!(action_for("profile", "K", &none, all()), Some("profile-up"));
        assert_eq!(action_for("profile", "Tab", &none, all()), Some("profile-next-card"));
        assert_eq!(action_for("profile", "Shift+Tab", &none, all()), Some("profile-previous-card"));
        assert_eq!(action_for("profile", "Return", &none, all()), Some("profile-enter"));
        assert_eq!(action_for("profile", "Escape", &none, all()), Some("escape"));
        assert_eq!(action_for("profile", "Ctrl+I", &none, all()), None, "the chats' keys stay out of the dialog");
        assert!(clashes(&none).is_empty(), "{:?}", clashes(&none));
        assert!(actions().iter().any(|(a, _)| *a == "profile-down") && !actions().iter().any(|(a, _)| *a == "profile-section"));
        // Bound to something else before Ctrl+I was the profile's: the
        // user's binding runs, from settings and from an exported profile.
        let mine = from_settings(&json!({"mute": {"main": {"add": ["Ctrl+I"]}}}));
        assert_eq!(action_for("composer", "Ctrl+I", &mine, all()), Some("mute"));
        assert!(!hints("pane", &mine, all()).iter().any(|b| b.action == "agent-profile"), "the bar does not offer a key that runs something else");
        assert!(yields("agent-profile", &mine) && shown("pane", "agent-profile", &mine).is_none());
        assert_eq!(import(&export(&mine)).unwrap(), mine);
        // And rebinding the profile works as for any action.
        let moved = take_over(&none, "agent-profile", "main", "Ctrl+Alt+P").unwrap();
        assert_eq!(action_for("pane", "Ctrl+Alt+P", &moved, all()), Some("agent-profile"));
        let gone = remove(&none, "agent-profile", "main", "Ctrl+I");
        assert_eq!(action_for("composer", "Ctrl+I", &gone, all()), None);
    }
}
