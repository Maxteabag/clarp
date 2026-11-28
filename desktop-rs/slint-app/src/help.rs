//! `?`: the keys that matter where the keyboard is (the chat, a card, the
//! explorer, a surface), a dozen or so in a few groups, spelled as the user
//! bound them, with the user's own bindings (double presses too) and a line
//! on where to find the rest.

use clarp_core::tr;

use crate::keymap::{self, Overrides};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub keys: String,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Group {
    pub title: String,
    pub items: Vec<Item>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Help {
    pub title: String,
    pub groups: Vec<Group>,
    pub footer: String,
}

/// How many of the user's own bindings the help lists.
const YOURS: usize = 5;

/// A key as the help spells it: in vim mode as Vim writes it (`f`, `J`).
fn spell(key: &str, vim: bool) -> String {
    if !vim {
        return keymap::display(key);
    }
    let one = |k: &str| keymap::display(&crate::vim::vim_key(k));
    match key.split_once(' ') {
        Some((first, _)) => format!("{} ×2", one(first)),
        None => one(key),
    }
}

/// An action's keys in `state` (the user's first, two at most).
fn keys(state: &str, action: &str, overrides: &Overrides, vim: bool) -> Option<String> {
    let keys = keymap::keys_in(state, action, overrides);
    (!keys.is_empty()).then(|| keys.iter().take(2).map(|k| spell(k, vim)).collect::<Vec<_>>().join(" / "))
}

struct Builder<'a> {
    state: &'a str,
    overrides: &'a Overrides,
    vim: bool,
    groups: Vec<Group>,
}

impl Builder<'_> {
    fn group(&mut self, title: String) {
        self.groups.push(Group { title, items: Vec::new() });
    }
    /// A fixed key (vim mode's, a control's own).
    fn fixed(&mut self, keys: &str, label: &str) {
        if let Some(group) = self.groups.last_mut() {
            group.items.push(Item { keys: keys.to_owned(), label: label.to_owned() });
        }
    }
    /// An action of the keyboard map, as bound; left out when it has no key.
    fn action(&mut self, action: &str, label: &str) {
        if let Some(keys) = keys(self.state, action, self.overrides, self.vim) {
            self.fixed(&keys, label);
        }
    }
}

/// The help for the map's `state`; `on_card` when the keyboard is on an
/// artifact card in the chat.
pub fn help(state: &str, on_card: bool, vim: bool, overrides: &Overrides) -> Help {
    let mut b = Builder { state, overrides, vim, groups: Vec::new() };
    let title = match (state, on_card) {
        // i18n: Keys help (?), title while the keyboard is on an artifact card in the chat
        ("pane", true) => tr!("Keys on a card"),
        // i18n: Keys help (?), title while the keyboard is on the chat
        ("pane", false) => tr!("Keys in the chat"),
        // i18n: Keys help (?), title while the keyboard is on the explorer (the list of chats)
        ("sidebar", _) => tr!("Keys in the explorer"),
        // i18n: Keys help (?), title elsewhere; {} is the key context's name (e.g. Settings, Updates)
        (other, _) => tr!("Keys in {}", keymap::context_name(other)),
    };
    match (state, on_card, vim) {
        ("pane", true, _) => {
            // i18n: Keys help (?), on a card in the chat: a group heading: keys for the artifact card the keyboard is on (noun)
            b.group(tr!("help group" => "Card"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the keys of `artifact-next`
            b.action("artifact-next", &tr!("Next card"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the keys of `artifact-previous`
            b.action("artifact-previous", &tr!("Previous card"));
            // i18n: Keys help (?), on a card in the chat: what a key does (verb), beside the keys of `artifact-open`
            b.action("artifact-open", &tr!("verb" => "Open"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the key "1-9"
            b.fixed("1-9", &tr!("Choose an answer"));
            // i18n: Keys help (?), on a card in the chat: what a key does (verb), beside the keys of `artifact-discard`
            b.action("artifact-discard", &tr!("verb" => "Discard"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the key "Left / Right"
            b.fixed("Left / Right", &tr!("Step or seek"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the keys of `artifact-stop`
            b.action("artifact-stop", &tr!("Stop a clip"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the key "Esc"
            b.fixed("Esc", &tr!("Leave the cards"));
            // i18n: Keys help (?), on a card in the chat: a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the key I (i in vim mode)
            b.fixed(if vim { "i" } else { "I" }, &tr!("Insert (type)"));
            // i18n: Keys help (?), on a card in the chat: what a key does, beside the keys of `link-hints`
            b.action("link-hints", &tr!("Open a link"));
        }
        ("pane", false, true) => {
            // i18n: Keys help (?), in the chat (vim mode): a group heading: keys that move or scroll within the chat or the list (verb)
            b.group(tr!("help group" => "Move"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "j / k"; "5j" is typed as it is
            b.fixed("j / k", &tr!("Scroll a line (5j: five)"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "d / u"
            b.fixed("d / u", &tr!("Half a page"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "gg / G"
            b.fixed("gg / G", &tr!("Top / latest"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "[ / ]"
            b.fixed("[ / ]", &tr!("Previous / next turn"));
            // i18n: Keys help (?), in the chat (vim mode): a group heading: keys that do something (verb)
            b.group(tr!("help group" => "Act"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "/  n  N"
            b.fixed("/  n  N", &tr!("Search the chat"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "y"
            b.fixed("y", &tr!("Copy the message"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "f"
            b.fixed("f", &tr!("Open a link"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does (noun), beside the key "zo / zc"
            b.fixed("zo / zc", &tr!("Tool activity"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does (noun), beside the key "J / K"
            b.fixed("J / K", &tr!("Cards"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "Space j"
            b.fixed("Space j", &tr!("Jobs and helpers"));
            // i18n: Keys help (?), in the chat (vim mode): a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "i"
            b.fixed("i", &tr!("Insert (type)"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does (noun), beside the key "h"
            b.fixed("h", &tr!("Explorer"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "gt / gT"
            b.fixed("gt / gT", &tr!("Next / previous tab"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does (noun), beside the key ":"
            b.fixed(":", &tr!("Command line"));
            // i18n: Keys help (?), in the chat (vim mode): what a key does, beside the key "Space"
            b.fixed("Space", &tr!("Every action (leader)"));
        }
        ("pane", false, false) => {
            // i18n: Keys help (?), in the chat: a group heading: keys that move or scroll within the chat or the list (verb)
            b.group(tr!("help group" => "Move"));
            // i18n: Keys help (?), in the chat: what a key does (verb), beside the key "Up / Down"
            b.fixed("Up / Down", &tr!("verb" => "Scroll"));
            // i18n: Keys help (?), in the chat: what a key does, beside the key "PgUp / PgDn"
            b.fixed("PgUp / PgDn", &tr!("A page"));
            // i18n: Keys help (?), in the chat: what a key does, beside the key "Home / End"
            b.fixed("Home / End", &tr!("Top / latest"));
            // i18n: Keys help (?), in the chat: a group heading: keys that do something (verb)
            b.group(tr!("help group" => "Act"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `link-hints`
            b.action("link-hints", &tr!("Open a link"));
            // i18n: Keys help (?), in the chat: what a key does (noun), beside the keys of `artifact-previous`
            b.action("artifact-previous", &tr!("Cards"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `stop-agent`
            b.action("stop-agent", &tr!("Stop the agent"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `agent-processes`
            b.action("agent-processes", &tr!("Jobs and helpers"));
            // i18n: Keys help (?), in the chat: what a key does (noun), beside the keys of `switcher`
            b.action("switcher", &tr!("Commands"));
            // i18n: Keys help (?), in the chat: a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `focus-composer`
            b.action("focus-composer", &tr!("Insert (type)"));
            // i18n: Keys help (?), in the chat: what a key does (noun), beside the keys of `focus-sidebar`
            b.action("focus-sidebar", &tr!("Explorer"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `next-attention`
            b.action("next-attention", &tr!("Next attention"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `split-right`
            b.action("split-right", &tr!("Split right"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `close-pane`
            b.action("close-pane", &tr!("Close pane"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `new-workspace`
            b.action("new-workspace", &tr!("New tab"));
            // i18n: Keys help (?), in the chat: what a key does, beside the keys of `next-workspace`
            b.action("next-workspace", &tr!("Next tab"));
        }
        ("sidebar", _, true) => {
            // i18n: Keys help (?), in the explorer (vim mode): a group heading: keys that move or scroll within the chat or the list (verb)
            b.group(tr!("help group" => "Move"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "j / k"
            b.fixed("j / k", &tr!("Next / previous chat"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "gg / G"
            b.fixed("gg / G", &tr!("First / last"));
            // i18n: Keys help (?), in the explorer (vim mode): a group heading: keys that do something (verb)
            b.group(tr!("help group" => "Act"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "l"
            b.fixed("l", &tr!("Open (or unfold)"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "Enter"
            b.fixed("Enter", &tr!("Open and type"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does (verb), beside the key "/"
            b.fixed("/", &tr!("verb" => "Filter"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does (noun), beside the key "p"
            b.fixed("p", &tr!("Live preview"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "zo / zc"
            b.fixed("zo / zc", &tr!("Unfold / fold"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the keys of `agent-processes`
            b.action("agent-processes", &tr!("Jobs and helpers"));
            // i18n: Keys help (?), in the explorer (vim mode): a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does (noun), beside the key "h"
            b.fixed("h", &tr!("Chat"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "i"
            b.fixed("i", &tr!("Insert (type)"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does (noun), beside the key ":"
            b.fixed(":", &tr!("Command line"));
            // i18n: Keys help (?), in the explorer (vim mode): what a key does, beside the key "Space"
            b.fixed("Space", &tr!("Every action (leader)"));
        }
        ("sidebar", _, false) => {
            // i18n: Keys help (?), in the explorer: a group heading: keys that move or scroll within the chat or the list (verb)
            b.group(tr!("help group" => "Move"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `agent-next`
            b.action("agent-next", &tr!("Next chat"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `agent-previous`
            b.action("agent-previous", &tr!("Previous chat"));
            // i18n: Keys help (?), in the explorer: a group heading: keys that do something (verb)
            b.group(tr!("help group" => "Act"));
            // i18n: Keys help (?), in the explorer: what a key does (verb), beside the keys of `agent-open`
            b.action("agent-open", &tr!("verb" => "Open"));
            // i18n: Keys help (?), in the explorer: what a key does (verb), beside the keys of `agent-search`
            b.action("agent-search", &tr!("verb" => "Filter"));
            // i18n: Keys help (?), in the explorer: what a key does (noun), beside the keys of `toggle-preview`
            b.action("toggle-preview", &tr!("Live preview"));
            // i18n: Keys help (?), in the explorer: what a key does (verb), beside the keys of `unfold`
            b.action("unfold", &tr!("verb" => "Unfold"));
            // i18n: Keys help (?), in the explorer: what a key does (verb), beside the keys of `fold`
            b.action("fold", &tr!("verb" => "Fold"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `agent-processes`
            b.action("agent-processes", &tr!("Jobs and helpers"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `new`
            b.action("new", &tr!("New agent"));
            // i18n: Keys help (?), in the explorer: a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), in the explorer: what a key does (noun), beside the keys of `focus-pane`
            b.action("focus-pane", &tr!("Chat"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `focus-composer`
            b.action("focus-composer", &tr!("Insert (type)"));
            // i18n: Keys help (?), in the explorer: what a key does, beside the keys of `sidebar`
            b.action("sidebar", &tr!("Hide the explorer"));
            // i18n: Keys help (?), in the explorer: what a key does (noun), beside the keys of `switcher`
            b.action("switcher", &tr!("Commands"));
        }
        (other, _, _) => {
            // i18n: Keys help (?), elsewhere: a group heading: keys that work in the place the keyboard is (adverb)
            b.group(tr!("help group" => "Here"));
            for binding in keymap::resolve(other, overrides, None).into_iter().filter(|x| x.hint && x.action != "help-keys").take(10) {
                b.action(binding.action, &binding.label);
            }
            // i18n: Keys help (?), elsewhere: a group heading: keys that move the keyboard somewhere else (verb, as in "go to")
            b.group(tr!("help group" => "Go"));
            // i18n: Keys help (?), elsewhere: what a key does (noun), beside the keys of `chats`
            b.action("chats", &tr!("Chats"));
            // i18n: Keys help (?), elsewhere: what a key does (noun), beside the keys of `switcher`
            b.action("switcher", &tr!("Commands"));
        }
    }
    // The user's own bindings that reach here.
    let mut yours = Vec::new();
    for (action, label) in keymap::actions() {
        let mine = keymap::user_keys(state, action, overrides);
        if !mine.is_empty() && yours.len() < YOURS {
            yours.push(Item { keys: mine.iter().take(2).map(|k| spell(k, vim)).collect::<Vec<_>>().join(" / "), label });
        }
    }
    if !yours.is_empty() {
        // i18n: Keys help (?), a group heading: the keys the user bound themselves (possessive, "your own")
        b.groups.push(Group { title: tr!("help group" => "Yours"), items: yours });
    }
    b.groups.retain(|g| !g.items.is_empty());
    let editor = keys(state, "edit-keymap", overrides, false).unwrap_or_default();
    let commands = keymap::keys_in(state, "switcher", overrides).into_iter().find(|k| k.starts_with("Ctrl+")).map(|k| keymap::display(&k)).unwrap_or_default();
    let footer = if vim {
        // i18n: Keys help (?), footer in vim mode: where to find the rest; {0} the key of the key-binding editor, {1} the key of the command palette; "Space k", "Space Space", ? and Esc are keys
        tr!("Everything else: Space k or {0} (key bindings)  ·  Space Space or {1} (commands)  ·  ? or Esc closes", editor, commands)
    } else {
        // i18n: Keys help (?), footer: where to find the rest; {0} the key of the key-binding editor, {1} the key of the command palette; ? and Esc are keys
        tr!("Everything else: {0} (key bindings)  ·  {1} (commands)  ·  ? or Esc closes", editor, commands)
    };
    Help { title, groups: b.groups, footer }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The keys as listed, read back as Ctrl (macOS shows Cmd).
    fn items(help: &Help) -> Vec<(String, String)> {
        help.groups.iter().flat_map(|g| g.items.iter().map(|i| (i.keys.replace("Cmd+", "Ctrl+"), i.label.clone()))).collect()
    }

    fn has(help: &Help, keys: &str, label: &str) -> bool {
        items(help).iter().any(|(k, l)| k == keys && l == label)
    }

    #[test]
    fn the_chat_the_explorer_and_a_card_each_get_their_own_short_list() {
        let none = Overrides::new();
        for vim in [true, false] {
            for (state, card) in [("pane", false), ("pane", true), ("sidebar", false)] {
                let help = help(state, card, vim, &none);
                let count = items(&help).len();
                assert!((8..=16).contains(&count), "{state} card {card} vim {vim}: {count} keys {:?}", items(&help));
                assert!(help.groups.len() >= 2, "grouped");
            }
        }
        let chat = help("pane", false, true, &none);
        assert_eq!(chat.title, "Keys in the chat");
        assert!(has(&chat, "y", "Copy the message") && has(&chat, "Space", "Every action (leader)"), "{:?}", items(&chat));
        let explorer = help("sidebar", false, true, &none);
        assert_eq!(explorer.title, "Keys in the explorer");
        assert!(has(&explorer, "l", "Open (or unfold)"));
        let card = help("pane", true, false, &none);
        assert!(has(&card, "O", "Open") && has(&card, "Esc", "Leave the cards"), "{:?}", items(&card));
        let classic = help("pane", false, false, &none);
        assert!(has(&classic, "F / Ctrl+L", "Open a link") && has(&classic, "Ctrl+T", "New tab"), "{:?}", items(&classic));
        // Where an agent's jobs are, from the chat and the explorer.
        assert!(has(&chat, "Space j", "Jobs and helpers"), "{:?}", items(&chat));
        assert!(has(&classic, "Ctrl+Shift+P", "Jobs and helpers"), "{:?}", items(&classic));
        assert!(has(&help("sidebar", false, false, &none), "Shift+P / Ctrl+Shift+P", "Jobs and helpers"));
        assert!(has(&explorer, "P / Ctrl+Shift+P", "Jobs and helpers"), "{:?}", items(&explorer));
        assert!(has(&help("updates", false, false, &none), "Ctrl+Shift+P", "Jobs"));
        let settings = help("settings", false, false, &none);
        assert_eq!(settings.title, "Keys in Settings");
        assert!(has(&settings, "Esc", "Back"), "{:?}", items(&settings));
    }

    #[test]
    fn the_user_s_own_keys_show_double_presses_included() {
        let taken = keymap::take_over(&Overrides::new(), "next-agent", "main", "Ctrl+J").unwrap();
        let mine = keymap::add(&taken, "next-agent", "main", "Right Right").unwrap();
        let shown = help("pane", false, true, &mine);
        let yours = shown.groups.iter().find(|g| g.title == "Yours").expect("a group of the user's own");
        assert_eq!(yours.items, [Item { keys: keymap::display("Ctrl+J") + " / Right ×2", label: "Next agent".into() }]);
        // A rebound action shows the user's key first.
        let link = keymap::add(&Overrides::new(), "link-hints", "main", "Ctrl+Shift+L").unwrap();
        assert!(has(&help("pane", false, false, &link), "Ctrl+Shift+L / F", "Open a link"));
    }

    #[test]
    fn the_footer_says_where_everything_is() {
        let none = Overrides::new();
        // Read as Ctrl (macOS shows Cmd).
        let vim = help("pane", false, true, &none).footer.replace("Cmd+", "Ctrl+");
        assert!(vim.contains("Space k") && vim.contains("Ctrl+K") && vim.contains("Ctrl+Alt+,"), "{vim}");
        let classic = help("sidebar", false, false, &none).footer.replace("Cmd+", "Ctrl+");
        assert!(classic.contains("Ctrl+K (commands)") && classic.contains("Esc closes"), "{classic}");
    }
}
