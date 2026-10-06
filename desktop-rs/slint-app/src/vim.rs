//! Vim mode: the keyboard without Ctrl. Outside a text field the chat and
//! the explorer are in Normal mode, where keys run on their own or in
//! short sequences (`gg`, `zo`, `Space w v`), with a count in front where
//! one makes sense (`3j`). `:` opens a command line (`:vs`, `:tabnew`,
//! `:theme hacker`) and `/` searches the chat. `i`, `a` and `o` go to the
//! composer (Insert), Escape comes back.
//!
//! Sequences are written as Vim writes them: a lowercase letter is the
//! key, an uppercase one is the key with Shift, `<Space>` the space bar.
//! Keys the vim map leaves alone keep the keyboard map's meaning, and the
//! user's own bindings win over both.

/// Where the keyboard is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Region {
    Chat,
    Explorer,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Place {
    Chat,
    Explorer,
    Both,
}

struct Entry {
    keys: &'static str,
    action: &'static str,
    label: &'static str,
    place: Place,
}

const fn entry(keys: &'static str, action: &'static str, label: &'static str, place: Place) -> Entry {
    Entry { keys, action, label, place }
}

/// Normal mode's keys.
const NORMAL: &[Entry] = &[
    entry("i", "vim-insert", "Insert", Place::Both),
    entry("a", "vim-insert", "Insert", Place::Both),
    entry("o", "vim-insert", "Insert", Place::Both),
    entry(":", "vim-command", "Command line", Place::Both),
    entry("g g", "vim-top", "Top", Place::Both),
    entry("G", "vim-bottom", "Bottom", Place::Both),
    entry("g t", "next-workspace", "Next tab", Place::Both),
    entry("g T", "previous-workspace", "Previous tab", Place::Both),
    // The chat.
    entry("j", "vim-down", "Down", Place::Chat),
    entry("k", "vim-up", "Up", Place::Chat),
    entry("d", "vim-half-down", "Half page down", Place::Chat),
    entry("u", "vim-half-up", "Half page up", Place::Chat),
    entry("]", "vim-turn-next", "Next turn", Place::Chat),
    entry("[", "vim-turn-previous", "Previous turn", Place::Chat),
    entry("/", "vim-search", "Search the chat", Place::Chat),
    entry("n", "vim-match-next", "Next match", Place::Chat),
    entry("N", "vim-match-previous", "Previous match", Place::Chat),
    entry("y", "vim-yank", "Copy the message", Place::Chat),
    entry("z o", "vim-fold-open", "Open", Place::Both),
    entry("z c", "vim-fold-close", "Close", Place::Both),
    entry("z a", "vim-fold-toggle", "Toggle", Place::Chat),
    entry("J", "artifact-next", "Next card", Place::Chat),
    entry("K", "artifact-previous", "Cards", Place::Chat),
    entry("h", "focus-sidebar", "Explorer", Place::Chat),
    // The explorer.
    entry("j", "agent-next", "Next", Place::Explorer),
    entry("k", "agent-previous", "Previous", Place::Explorer),
    entry("l", "vim-open", "Open", Place::Explorer),
    // The leader: every action of the window, by a mnemonic.
    entry("<Space> <Space>", "switcher", "Commands", Place::Both),
    entry("<Space> :", "vim-command", "Command line", Place::Both),
    entry("<Space> e", "focus-sidebar", "Explorer", Place::Both),
    entry("<Space> b", "sidebar", "Show/hide explorer", Place::Both),
    entry("<Space> c", "focus-pane", "Chat", Place::Both),
    entry("<Space> n", "new", "New agent", Place::Both),
    entry("<Space> N", "new-contact", "Start contact", Place::Both),
    entry("<Space> s", "settings", "Settings", Place::Both),
    entry("<Space> k", "edit-keymap", "Key bindings", Place::Both),
    entry("<Space> t", "new-workspace", "New tab", Place::Both),
    entry("<Space> r", "recent-agents", "Recent", Place::Both),
    entry("<Space> /", "search-messages", "Search all chats", Place::Both),
    entry("<Space> a", "next-attention", "Next attention", Place::Both),
    entry("<Space> u", "updates", "Updates", Place::Both),
    entry("<Space> T", "teams", "Teams", Place::Both),
    entry("<Space> o", "overview", "Overview", Place::Both),
    entry("<Space> p", "agent-profile", "Agent profile", Place::Both),
    entry("<Space> d", "change-directory", "Change directory", Place::Both),
    entry("<Space> R", "rename-agent", "Rename contact", Place::Both),
    entry("<Space> m", "mute", "Mute", Place::Both),
    entry("<Space> x", "stop-agent", "Stop agent", Place::Both),
    entry("<Space> l", "link-hints", "Open a link", Place::Both),
    entry("<Space> ?", "shortcut-bar", "Show/hide keys", Place::Both),
    entry("<Space> w n", "new-workspace", "New tab", Place::Both),
    entry("<Space> w ]", "next-workspace", "Next tab", Place::Both),
    entry("<Space> w [", "previous-workspace", "Previous tab", Place::Both),
    entry("<Space> w c", "close-workspace", "Close tab", Place::Both),
    entry("<Space> w v", "split-right", "Split right", Place::Both),
    entry("<Space> w s", "split-down", "Split down", Place::Both),
    entry("<Space> w q", "close-pane", "Close pane", Place::Both),
    entry("<Space> w z", "zoom", "Zoom pane", Place::Both),
    entry("<Space> w =", "balance", "Balance panes", Place::Both),
    entry("<Space> w h", "move-left", "Left pane", Place::Both),
    entry("<Space> w j", "move-down", "Lower pane", Place::Both),
    entry("<Space> w k", "move-up", "Upper pane", Place::Both),
    entry("<Space> w l", "move-right", "Right pane", Place::Both),
];

/// What the keys before the next one mean, for the which-key panel.
const GROUPS: &[(&str, &str)] = &[("g", "Go"), ("z", "Fold"), ("<Space>", "Leader"), ("<Space> w", "Tabs and panes")];

/// The keys a card the keyboard is on keeps (open, choose, stop, walk).
const CARD_KEYS: &[&str] = &["J", "K", "O", "S", "Delete", "Left", "Right", "1", "2", "3", "4", "5", "6", "7", "8", "9"];

/// A Vim key as the keyboard map spells it: `j` is "J", `J` is "Shift+J".
pub fn chord_of(token: &str) -> String {
    match token {
        "<Space>" => "Space".to_owned(),
        "<CR>" => "Return".to_owned(),
        "<Esc>" => "Escape".to_owned(),
        _ => {
            let mut chars = token.chars();
            match (chars.next(), chars.next()) {
                (Some(c), None) if c.is_ascii_lowercase() => c.to_ascii_uppercase().to_string(),
                (Some(c), None) if c.is_ascii_uppercase() => format!("Shift+{c}"),
                _ => token.to_owned(),
            }
        }
    }
}

/// A key as Vim writes it, for the which-key panel and the shortcut bar.
pub fn vim_key(chord: &str) -> String {
    match chord.strip_prefix("Shift+") {
        Some(letter) if letter.len() == 1 && letter.chars().all(|c| c.is_ascii_alphabetic()) => letter.to_owned(),
        _ if chord.len() == 1 && chord.chars().all(|c| c.is_ascii_alphabetic()) => chord.to_ascii_lowercase(),
        _ => chord.to_owned(),
    }
}

fn chords(keys: &str) -> Vec<String> {
    keys.split(' ').map(chord_of).collect()
}

fn here(place: Place, region: Region) -> bool {
    matches!((place, region), (Place::Both, _) | (Place::Chat, Region::Chat) | (Place::Explorer, Region::Explorer))
}

/// What a key in Normal mode did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The start of a longer sequence (or a count): more keys follow.
    Pending,
    /// Run the action; `count` when one was typed in front.
    Run { action: &'static str, count: Option<u32> },
    /// Not a vim key here: the keyboard map has it.
    Pass,
    /// A sequence was dropped (Escape, or a key that continues nothing).
    Cancelled,
}

/// Normal mode: the keys of a sequence so far and the count in front.
#[derive(Debug, Default)]
pub struct Normal {
    keys: Vec<String>,
    count: Option<u32>,
}

impl Normal {
    /// One key. `on_card`: the keyboard is on an artifact card, whose own
    /// keys it keeps.
    pub fn feed(&mut self, region: Region, chord: &str, on_card: bool) -> Outcome {
        if chord == "Escape" {
            let pending = self.pending();
            self.reset();
            return if pending { Outcome::Cancelled } else { Outcome::Pass };
        }
        if self.keys.is_empty() {
            if on_card && CARD_KEYS.contains(&chord) {
                self.reset();
                return Outcome::Pass;
            }
            // A count: 1 to 9 start it, 0 only goes on with one.
            if let Some(digit) = chord.parse::<u32>().ok().filter(|d| chord.len() == 1 && (*d > 0 || self.count.is_some())) {
                self.count = Some((self.count.unwrap_or(0) * 10 + digit).min(9999));
                return Outcome::Pending;
            }
        }
        self.keys.push(chord.to_owned());
        let mut longer = false;
        let mut exact = None;
        for entry in NORMAL.iter().filter(|e| here(e.place, region)) {
            let keys = chords(entry.keys);
            if keys.starts_with(&self.keys) {
                if keys.len() == self.keys.len() {
                    exact = Some(entry.action);
                } else {
                    longer = true;
                }
            }
        }
        if longer {
            return Outcome::Pending;
        }
        let started = self.keys.len() > 1;
        let count = self.count;
        self.reset();
        match exact {
            Some(action) => Outcome::Run { action, count },
            None if started => Outcome::Cancelled,
            None => Outcome::Pass,
        }
    }

    /// Whether a sequence or a count is under way.
    pub fn pending(&self) -> bool {
        !self.keys.is_empty() || self.count.is_some()
    }

    /// The keys so far, as Vim writes them ("3", "Space w").
    pub fn typed(&self) -> String {
        let count = self.count.map(|c| c.to_string());
        count.into_iter().chain(self.keys.iter().map(|k| vim_key(k))).collect::<Vec<_>>().join(" ")
    }

    /// The keys that may follow, with what they do: the which-key panel.
    pub fn next(&self, region: Region) -> Vec<(String, &'static str)> {
        if self.keys.is_empty() {
            return Vec::new();
        }
        let mut out: Vec<(String, &'static str)> = Vec::new();
        for entry in NORMAL.iter().filter(|e| here(e.place, region)) {
            let keys = chords(entry.keys);
            if keys.len() <= self.keys.len() || !keys.starts_with(&self.keys) {
                continue;
            }
            let key = vim_key(&keys[self.keys.len()]);
            if out.iter().any(|(k, _)| *k == key) {
                continue;
            }
            let label = if keys.len() == self.keys.len() + 1 {
                entry.label
            } else {
                let prefix: Vec<&str> = entry.keys.split(' ').take(self.keys.len() + 1).collect();
                GROUPS.iter().find(|(g, _)| *g == prefix.join(" ")).map_or("More", |(_, label)| *label)
            };
            out.push((key, label));
        }
        out
    }

    pub fn reset(&mut self) {
        self.keys.clear();
        self.count = None;
    }
}

/// An ex command, parsed.
#[derive(Debug, Clone, PartialEq)]
pub enum Ex {
    /// One of the keyboard map's actions.
    Run(&'static str),
    /// A new tab (workspace), with a name or none.
    TabNew(String),
    /// Opens the agent whose name starts with this.
    Open(String),
    Theme(String),
    Font(Font),
    /// `:set` and what follows, for the preferences (`pref-set:`).
    Set(String),
    /// Clears the chat search.
    NoHighlight,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Font {
    /// The reading theme's font picker.
    Picker,
    /// Back to the reading theme's own font.
    ThemeDefault,
    /// The chats' zoom: a step larger, smaller, 100%, a percentage.
    Larger,
    Smaller,
    Reset,
    Zoom(f64),
}

/// The preferences `:set` knows, with their other names (Tab offers them
/// with `no` and `=`).
fn preferences() -> impl Iterator<Item = &'static clarp_core::prefs::Spec> {
    clarp_core::prefs::specs().iter()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Run(&'static str),
    TabNew,
    Open,
    Theme,
    Font,
    Set,
    NoHighlight,
}

/// The commands: the whole name, the shortest it may be cut to, and what it
/// does. The first that matches wins, as in Vim (`:s` is not `:settings`).
const COMMANDS: &[(&str, usize, Kind)] = &[
    ("quit", 1, Kind::Run("close-pane")),
    ("close", 3, Kind::Run("close-pane")),
    ("vsplit", 2, Kind::Run("split-right")),
    ("split", 2, Kind::Run("split-down")),
    ("only", 2, Kind::Run("zoom")),
    ("zoom", 4, Kind::Run("zoom")),
    ("balance", 3, Kind::Run("balance")),
    ("tabnew", 6, Kind::TabNew),
    ("tabedit", 4, Kind::TabNew),
    ("tabnext", 4, Kind::Run("next-workspace")),
    ("tabprevious", 4, Kind::Run("previous-workspace")),
    ("tabNext", 4, Kind::Run("previous-workspace")),
    ("tabclose", 4, Kind::Run("close-workspace")),
    ("edit", 1, Kind::Open),
    ("buffer", 1, Kind::Open),
    ("theme", 3, Kind::Theme),
    ("colorscheme", 4, Kind::Theme),
    ("font", 4, Kind::Font),
    ("set", 2, Kind::Set),
    ("settings", 8, Kind::Run("settings")),
    ("keymap", 4, Kind::Run("edit-keymap")),
    ("help", 1, Kind::Run("edit-keymap")),
    ("new", 3, Kind::Run("new")),
    ("explorer", 3, Kind::Run("focus-sidebar")),
    ("recent", 3, Kind::Run("recent-agents")),
    ("updates", 2, Kind::Run("updates")),
    ("teams", 4, Kind::Run("teams")),
    ("overview", 4, Kind::Run("overview")),
    ("commands", 4, Kind::Run("switcher")),
    ("nohlsearch", 3, Kind::NoHighlight),
];

fn command(word: &str) -> Option<Kind> {
    COMMANDS.iter().find(|(name, _, _)| *name == word).or_else(|| COMMANDS.iter().find(|(name, least, _)| word.len() >= *least && name.starts_with(word))).map(|(_, _, kind)| *kind)
}

fn font(argument: &str) -> Result<Font, String> {
    match argument {
        "" => Ok(Font::Picker),
        "reset" | "default" => Ok(Font::ThemeDefault),
        "+" | "larger" => Ok(Font::Larger),
        "-" | "smaller" => Ok(Font::Smaller),
        "=" | "0" => Ok(Font::Reset),
        other => other.trim_end_matches('%').parse::<f64>().ok().filter(|f| f.is_finite() && *f > 0.0).map(Font::Zoom).ok_or_else(|| format!("Not a font command: {other} (:font picks one; reset, +, -, = or a zoom such as 120)")),
    }
}

fn set(argument: &str) -> Result<Ex, String> {
    if argument.is_empty() {
        return Err("Set what? (:set novim, :set fontsize=16, :set theme=night)".into());
    }
    match argument.split_once('=').map(|(name, value)| (name.trim(), value.trim())) {
        Some(("theme" | "colorscheme", value)) => Ok(Ex::Theme(value.to_owned())),
        _ => Ok(Ex::Set(argument.to_owned())),
    }
}

/// Parses a command line (without its `:`).
pub fn parse(line: &str) -> Result<Ex, String> {
    let line = line.trim();
    let (word, argument) = line.split_once(char::is_whitespace).map_or((line, ""), |(w, a)| (w, a.trim()));
    let word = word.trim_end_matches('!');
    if word.is_empty() {
        return Err("No command".into());
    }
    let Some(kind) = command(word) else { return Err(format!("Not a command: {word}")) };
    match kind {
        Kind::Run(action) => Ok(Ex::Run(action)),
        Kind::TabNew => Ok(Ex::TabNew(argument.to_owned())),
        Kind::Open if argument.is_empty() => Ok(Ex::Run("switcher")),
        Kind::Open => Ok(Ex::Open(argument.to_owned())),
        Kind::Theme if argument.is_empty() => Err("Which theme? (Tab lists them)".into()),
        Kind::Theme => Ok(Ex::Theme(argument.to_owned())),
        Kind::Font => font(argument).map(Ex::Font),
        Kind::Set => set(argument),
        Kind::NoHighlight => Ok(Ex::NoHighlight),
    }
}

/// What Tab offers for a command line: whole lines, the typed one first
/// matched. `agents` and `themes` complete their commands' arguments.
pub fn complete(line: &str, agents: &[String], themes: &[String]) -> Vec<String> {
    let Some((word, argument)) = line.split_once(' ') else {
        let mut names: Vec<String> = Vec::new();
        for (name, _, _) in COMMANDS {
            if name.starts_with(line) && !names.iter().any(|n| n == name) {
                names.push((*name).to_owned());
            }
        }
        return names;
    };
    let argument = argument.trim_start();
    let starts = |candidate: &str| candidate.to_lowercase().starts_with(&argument.to_lowercase());
    let words: Vec<String> = match command(word) {
        Some(Kind::Theme) => themes.iter().filter(|t| starts(t)).cloned().collect(),
        Some(Kind::Open) => agents.iter().filter(|a| starts(a)).cloned().collect(),
        Some(Kind::Font) => ["reset", "+", "-", "="].iter().filter(|f| starts(f)).map(|f| (*f).to_owned()).collect(),
        Some(Kind::Set) => {
            let mut all: Vec<String> = preferences().map(|spec| spec.name.to_owned()).collect();
            all.extend(preferences().filter(|spec| matches!(spec.kind, clarp_core::prefs::Kind::Toggle { .. })).map(|spec| format!("no{}", spec.name)));
            all.extend(themes.iter().map(|t| format!("theme={t}")));
            all.into_iter().filter(|o| starts(o)).collect()
        }
        _ => Vec::new(),
    };
    words.into_iter().map(|w| format!("{word} {w}")).collect()
}

/// The command line (`:`) or the chat search (`/`) while it is open.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    pub kind: char,
    pub text: String,
    /// What Tab offers, and the one shown.
    pub choices: Vec<String>,
    pub chosen: Option<usize>,
}

/// What a key did to the line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LineOutcome {
    Editing,
    Done(String),
    Cancelled,
}

impl Line {
    pub fn new(kind: char) -> Self {
        Self { kind, text: String::new(), choices: Vec::new(), chosen: None }
    }

    /// One key: `text` is what it types. `complete` gives Tab's choices.
    pub fn key(&mut self, chord: &str, text: &str, complete: impl Fn(&str) -> Vec<String>) -> LineOutcome {
        match chord {
            "Escape" => return LineOutcome::Cancelled,
            "Return" => return LineOutcome::Done(self.text.clone()),
            "Backspace" if self.text.is_empty() => return LineOutcome::Cancelled,
            "Backspace" => {
                self.text.pop();
                self.choices.clear();
                self.chosen = None;
            }
            "Tab" | "Shift+Tab" => {
                if self.choices.is_empty() {
                    self.choices = complete(&self.text);
                    self.chosen = None;
                }
                let count = self.choices.len();
                if count > 0 {
                    let next = match (self.chosen, chord == "Tab") {
                        (None, true) => 0,
                        (None, false) => count - 1,
                        (Some(i), true) => (i + 1) % count,
                        (Some(i), false) => (i + count - 1) % count,
                    };
                    self.chosen = Some(next);
                    self.text = self.choices[next].clone();
                }
            }
            _ if chord.starts_with("Ctrl+") || chord.contains("Alt+") => {}
            _ => {
                let mut chars = text.chars();
                if let (Some(c), None) = (chars.next(), chars.next()) {
                    if !c.is_control() {
                        self.text.push(c);
                        self.choices.clear();
                        self.chosen = None;
                    }
                }
            }
        }
        LineOutcome::Editing
    }
}

/// The row a search for `pattern` finds after `from` (before it, going
/// back), round the end; smart case: a capital letter matches case.
pub fn find(texts: &[String], pattern: &str, from: Option<usize>, forward: bool) -> Option<usize> {
    let count = texts.len();
    if pattern.is_empty() || count == 0 {
        return None;
    }
    let exact = pattern.chars().any(char::is_uppercase);
    let lower = pattern.to_lowercase();
    let matches = |text: &String| if exact { text.contains(pattern) } else { text.to_lowercase().contains(&lower) };
    let start = match (from, forward) {
        (Some(i), true) => i + 1,
        (Some(i), false) => i + count - 1,
        (None, true) => 0,
        (None, false) => count - 1,
    };
    (0..count).map(|step| if forward { (start + step) % count } else { (start + count * 2 - step) % count }).find(|i| matches(&texts[*i]))
}

/// The turn (a row of the user's) after `from`, or before it going back.
pub fn turn(authors: &[String], from: Option<usize>, forward: bool) -> Option<usize> {
    let user = |i: &usize| authors[*i] == "user";
    match (from, forward) {
        (Some(at), true) => (at + 1..authors.len()).find(user),
        (Some(at), false) => (0..at.min(authors.len())).rev().find(user),
        (None, true) => (0..authors.len()).find(user),
        (None, false) => (0..authors.len()).rev().find(user),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn feed(normal: &mut Normal, region: Region, keys: &[&str]) -> Vec<Outcome> {
        keys.iter().map(|k| normal.feed(region, k, false)).collect()
    }

    fn run(action: &'static str) -> Outcome {
        Outcome::Run { action, count: None }
    }

    #[test]
    fn vim_keys_are_spelled_like_the_map() {
        assert_eq!(chord_of("j"), "J");
        assert_eq!(chord_of("J"), "Shift+J");
        assert_eq!(chord_of("<Space>"), "Space");
        assert_eq!(chord_of("["), "[");
        assert_eq!(chord_of("3"), "3");
        assert_eq!(vim_key("J"), "j");
        assert_eq!(vim_key("Shift+G"), "G");
        assert_eq!(vim_key("Space"), "Space");
        assert_eq!(vim_key("/"), "/");
    }

    #[test]
    fn single_keys_and_sequences_run_their_actions() {
        let mut normal = Normal::default();
        assert_eq!(normal.feed(Region::Chat, "J", false), run("vim-down"));
        assert_eq!(normal.feed(Region::Chat, "K", false), run("vim-up"));
        assert_eq!(normal.feed(Region::Chat, "Shift+G", false), run("vim-bottom"));
        assert_eq!(feed(&mut normal, Region::Chat, &["G", "G"]), [Outcome::Pending, run("vim-top")]);
        assert_eq!(feed(&mut normal, Region::Chat, &["G", "T"]), [Outcome::Pending, run("next-workspace")]);
        assert_eq!(feed(&mut normal, Region::Chat, &["G", "Shift+T"]), [Outcome::Pending, run("previous-workspace")]);
        assert_eq!(feed(&mut normal, Region::Chat, &["Z", "O"]), [Outcome::Pending, run("vim-fold-open")]);
        assert_eq!(normal.feed(Region::Chat, "/", false), run("vim-search"));
        assert_eq!(normal.feed(Region::Chat, ":", false), run("vim-command"));
        assert_eq!(normal.feed(Region::Chat, "Y", false), run("vim-yank"));
        assert_eq!(normal.feed(Region::Chat, "]", false), run("vim-turn-next"));
        assert_eq!(normal.feed(Region::Chat, "H", false), run("focus-sidebar"));
        for key in ["I", "A", "O"] {
            assert_eq!(normal.feed(Region::Chat, key, false), run("vim-insert"), "{key} inserts");
        }
        // Capital J and K walk the cards; small j and k scroll.
        assert_eq!(normal.feed(Region::Chat, "Shift+K", false), run("artifact-previous"));
        assert_eq!(normal.feed(Region::Chat, "Shift+J", false), run("artifact-next"));
        assert!(!normal.pending());
    }

    #[test]
    fn the_leader_reaches_every_window_action_without_ctrl() {
        let mut normal = Normal::default();
        for (keys, action) in [
            (&["Space", "Space"][..], "switcher"),
            (&["Space", "E"], "focus-sidebar"),
            (&["Space", "N"], "new"),
            (&["Space", "S"], "settings"),
            (&["Space", "K"], "edit-keymap"),
            (&["Space", "T"], "new-workspace"),
            (&["Space", "W", "V"], "split-right"),
            (&["Space", "W", "S"], "split-down"),
            (&["Space", "W", "Q"], "close-pane"),
            (&["Space", "W", "L"], "move-right"),
            (&["Space", "W", "C"], "close-workspace"),
            (&["Space", "/"], "search-messages"),
        ] {
            let outcomes = feed(&mut normal, Region::Chat, keys);
            assert_eq!(outcomes.last(), Some(&run(action)), "{keys:?}");
            assert!(outcomes[..keys.len() - 1].iter().all(|o| *o == Outcome::Pending), "{keys:?}: {outcomes:?}");
        }
        // The same from the explorer.
        assert_eq!(feed(&mut normal, Region::Explorer, &["Space", "S"]).last(), Some(&run("settings")));
    }

    #[test]
    fn which_key_lists_what_may_follow() {
        let mut normal = Normal::default();
        assert_eq!(normal.feed(Region::Chat, "Space", false), Outcome::Pending);
        assert_eq!(normal.typed(), "Space");
        let next = normal.next(Region::Chat);
        assert!(next.contains(&("e".to_owned(), "Explorer")), "{next:?}");
        assert!(next.contains(&("w".to_owned(), "Tabs and panes")), "a group says what it holds: {next:?}");
        assert!(next.contains(&("Space".to_owned(), "Commands")), "{next:?}");
        assert_eq!(normal.feed(Region::Chat, "W", false), Outcome::Pending);
        assert_eq!(normal.typed(), "Space w");
        assert!(normal.next(Region::Chat).contains(&("v".to_owned(), "Split right")));
        assert_eq!(normal.feed(Region::Chat, "Escape", false), Outcome::Cancelled, "Escape drops the sequence");
        assert!(!normal.pending() && normal.next(Region::Chat).is_empty());
        // A key that continues nothing ends the sequence, and is used up.
        assert_eq!(feed(&mut normal, Region::Chat, &["G", "X"]), [Outcome::Pending, Outcome::Cancelled]);
        assert!(!normal.pending());
    }

    #[test]
    fn counts_go_in_front() {
        let mut normal = Normal::default();
        assert_eq!(feed(&mut normal, Region::Chat, &["3", "J"]), [Outcome::Pending, Outcome::Run { action: "vim-down", count: Some(3) }]);
        assert_eq!(feed(&mut normal, Region::Chat, &["1", "2", "K"]).last(), Some(&Outcome::Run { action: "vim-up", count: Some(12) }));
        assert_eq!(feed(&mut normal, Region::Chat, &["2", "0", "J"]).last(), Some(&Outcome::Run { action: "vim-down", count: Some(20) }));
        assert_eq!(feed(&mut normal, Region::Chat, &["2", "G", "T"]).last(), Some(&Outcome::Run { action: "next-workspace", count: Some(2) }), "2gt is the second tab");
        assert_eq!(feed(&mut normal, Region::Explorer, &["3", "J"]).last(), Some(&Outcome::Run { action: "agent-next", count: Some(3) }));
        // 0 alone is no count; a count before a key vim does not have is dropped.
        assert_eq!(normal.feed(Region::Chat, "0", false), Outcome::Pass);
        assert_eq!(feed(&mut normal, Region::Chat, &["4", "F"]), [Outcome::Pending, Outcome::Pass]);
        assert!(!normal.pending());
    }

    #[test]
    fn a_card_keeps_its_keys_and_other_keys_pass() {
        let mut normal = Normal::default();
        for key in ["J", "K", "O", "S", "2", "Right"] {
            assert_eq!(normal.feed(Region::Chat, key, true), Outcome::Pass, "{key} is the card's");
        }
        // Keys the vim map does not have stay the keyboard map's.
        for key in ["F", "E", "P", "Return", "Tab", "Escape", "Up", "Ctrl+K"] {
            assert_eq!(normal.feed(Region::Chat, key, false), Outcome::Pass, "{key}");
        }
        // The explorer's own: / filters and p previews there; l opens.
        assert_eq!(normal.feed(Region::Explorer, "/", false), Outcome::Pass);
        assert_eq!(normal.feed(Region::Explorer, "P", false), Outcome::Pass);
        assert_eq!(normal.feed(Region::Explorer, "L", false), run("vim-open"));
        assert_eq!(normal.feed(Region::Explorer, "J", false), run("agent-next"));
        assert_eq!(feed(&mut normal, Region::Explorer, &["Z", "C"]).last(), Some(&run("vim-fold-close")));
    }

    #[test]
    fn ex_commands_parse() {
        assert_eq!(parse("q"), Ok(Ex::Run("close-pane")));
        assert_eq!(parse("quit"), Ok(Ex::Run("close-pane")));
        assert_eq!(parse("vs"), Ok(Ex::Run("split-right")));
        assert_eq!(parse("vsplit"), Ok(Ex::Run("split-right")));
        assert_eq!(parse("sp"), Ok(Ex::Run("split-down")));
        assert_eq!(parse("tabnew"), Ok(Ex::TabNew(String::new())));
        assert_eq!(parse("tabnew  Review "), Ok(Ex::TabNew("Review".into())));
        assert_eq!(parse("tabn"), Ok(Ex::Run("next-workspace")));
        assert_eq!(parse("tabp"), Ok(Ex::Run("previous-workspace")));
        assert_eq!(parse("tabc"), Ok(Ex::Run("close-workspace")));
        assert_eq!(parse(" theme hacker"), Ok(Ex::Theme("hacker".into())));
        assert_eq!(parse("colo paper"), Ok(Ex::Theme("paper".into())));
        assert_eq!(parse("font +"), Ok(Ex::Font(Font::Larger)));
        assert_eq!(parse("font -"), Ok(Ex::Font(Font::Smaller)));
        assert_eq!(parse("font"), Ok(Ex::Font(Font::Picker)));
        assert_eq!(parse("font reset"), Ok(Ex::Font(Font::ThemeDefault)));
        assert_eq!(parse("font ="), Ok(Ex::Font(Font::Reset)));
        assert!(parse("font huge").is_err());
        // :set goes to the preferences as typed, which know every name.
        assert_eq!(parse("set novim"), Ok(Ex::Set("novim".into())));
        assert_eq!(parse("set fontsize+=2"), Ok(Ex::Set("fontsize+=2".into())));
        assert_eq!(parse("se timestamps!"), Ok(Ex::Set("timestamps!".into())));
        assert_eq!(parse("font 120%"), Ok(Ex::Font(Font::Zoom(120.0))));
        assert_eq!(parse("set theme=night"), Ok(Ex::Theme("night".into())));
        assert_eq!(parse("e rach"), Ok(Ex::Open("rach".into())));
        assert_eq!(parse("b Mike"), Ok(Ex::Open("Mike".into())));
        assert_eq!(parse("settings"), Ok(Ex::Run("settings")));
        assert_eq!(parse("noh"), Ok(Ex::NoHighlight));
        assert_eq!(parse("only"), Ok(Ex::Run("zoom")));
        assert_eq!(parse("help"), Ok(Ex::Run("edit-keymap")));
        assert_eq!(parse("frobnicate").unwrap_err(), "Not a command: frobnicate");
        assert!(parse("").is_err());
    }

    #[test]
    fn tab_completes_commands_and_their_arguments() {
        let agents = vec!["Rachel".to_owned(), "Mike".to_owned()];
        let themes = vec!["hacker".to_owned(), "paper".to_owned(), "night".to_owned()];
        let tab = complete("tab", &agents, &themes);
        assert!(tab.contains(&"tabnew".to_owned()) && tab.contains(&"tabnext".to_owned()), "{tab:?}");
        assert_eq!(complete("theme h", &agents, &themes), ["theme hacker"]);
        assert_eq!(complete("colo ", &agents, &themes), ["colo hacker", "colo paper", "colo night"]);
        assert_eq!(complete("e ra", &agents, &themes), ["e Rachel"], "agent names, any case");
        assert!(complete("set no", &agents, &themes).contains(&"set novim".to_owned()));
        assert!(complete("set ti", &agents, &themes).contains(&"set timestamps".to_owned()));
        assert!(complete("zzz", &agents, &themes).is_empty());
    }

    #[test]
    fn the_line_edits_completes_and_ends() {
        let choices = |text: &str| complete(text, &[], &["hacker".to_owned(), "hyperlegible".to_owned()]);
        let mut line = Line::new(':');
        for c in "theme h".chars() {
            let chord = if c == ' ' { "Space".to_owned() } else { c.to_uppercase().to_string() };
            assert_eq!(line.key(&chord, &c.to_string(), choices), LineOutcome::Editing);
        }
        assert_eq!(line.text, "theme h");
        line.key("Tab", "\t", choices);
        assert_eq!(line.text, "theme hacker");
        line.key("Tab", "\t", choices);
        assert_eq!(line.text, "theme hyperlegible", "Tab again: the next choice");
        line.key("Shift+Tab", "", choices);
        assert_eq!(line.text, "theme hacker", "Shift+Tab: back");
        line.key("Backspace", "", choices);
        assert_eq!(line.text, "theme hacke");
        assert!(line.choices.is_empty(), "editing forgets the choices");
        assert_eq!(line.key("Return", "\n", choices), LineOutcome::Done("theme hacke".into()));
        let mut empty = Line::new('/');
        assert_eq!(empty.key("Backspace", "", choices), LineOutcome::Cancelled, "Backspace on nothing closes it");
        let mut typed = Line::new('/');
        typed.key("X", "x", choices);
        assert_eq!(typed.key("Escape", "", choices), LineOutcome::Cancelled);
        // Ctrl and Alt chords type nothing.
        let mut held = Line::new(':');
        held.key("Ctrl+K", "k", choices);
        assert_eq!(held.text, "");
    }

    #[test]
    fn search_finds_the_next_row_round_the_end_with_smart_case() {
        let texts: Vec<String> = ["hello world", "Deploy the Host", "nothing", "deploy again"].iter().map(|s| (*s).to_owned()).collect();
        assert_eq!(find(&texts, "deploy", None, true), Some(1));
        assert_eq!(find(&texts, "deploy", Some(1), true), Some(3));
        assert_eq!(find(&texts, "deploy", Some(3), true), Some(1), "round the end");
        assert_eq!(find(&texts, "deploy", Some(3), false), Some(1));
        assert_eq!(find(&texts, "deploy", None, false), Some(3), "going back starts at the end");
        assert_eq!(find(&texts, "Deploy", Some(1), true), Some(1), "a capital matches case");
        assert_eq!(find(&texts, "absent", None, true), None);
        assert_eq!(find(&texts, "", None, true), None);
    }

    #[test]
    fn turns_are_the_user_s_rows() {
        let authors: Vec<String> = ["user", "activity", "assistant", "user", "assistant", "user", "assistant"].iter().map(|s| (*s).to_owned()).collect();
        assert_eq!(turn(&authors, Some(0), true), Some(3));
        assert_eq!(turn(&authors, Some(3), true), Some(5));
        assert_eq!(turn(&authors, Some(5), true), None, "no turn after the last");
        assert_eq!(turn(&authors, Some(4), false), Some(3));
        assert_eq!(turn(&authors, Some(3), false), Some(0));
        assert_eq!(turn(&authors, None, false), Some(5), "from the end, the last turn");
        assert_eq!(turn(&authors, None, true), Some(0));
    }
}
