//! `?`: the keys that matter where the keyboard is (the chat, a card, the
//! explorer, a surface), a dozen or so in a few groups, spelled as the user
//! bound them, with the user's own bindings (double presses too) and a line
//! on where to find the rest.

use crate::keymap::{self, Overrides};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub keys: String,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Group {
    pub title: &'static str,
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
    fn group(&mut self, title: &'static str) {
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
        ("pane", true) => "Keys on a card".to_owned(),
        ("pane", false) => "Keys in the chat".to_owned(),
        ("sidebar", _) => "Keys in the explorer".to_owned(),
        (other, _) => format!("Keys in {}", keymap::context_name(other)),
    };
    match (state, on_card, vim) {
        ("pane", true, _) => {
            b.group("Card");
            b.action("artifact-next", "Next card");
            b.action("artifact-previous", "Previous card");
            b.action("artifact-open", "Open");
            b.fixed("1-9", "Choose an answer");
            b.action("artifact-discard", "Discard");
            b.fixed("Left / Right", "Step or seek");
            b.action("artifact-stop", "Stop a clip");
            b.fixed("Esc", "Leave the cards");
            b.group("Go");
            b.fixed(if vim { "i" } else { "I" }, "Insert (type)");
            b.action("link-hints", "Open a link");
        }
        ("pane", false, true) => {
            b.group("Move");
            b.fixed("j / k", "Scroll a line (5j: five)");
            b.fixed("d / u", "Half a page");
            b.fixed("gg / G", "Top / latest");
            b.fixed("[ / ]", "Previous / next turn");
            b.group("Act");
            b.fixed("/  n  N", "Search the chat");
            b.fixed("y", "Copy the message");
            b.fixed("f", "Open a link");
            b.fixed("zo / zc", "Tool activity");
            b.fixed("J / K", "Cards");
            b.fixed("Space j", "Jobs and helpers");
            b.group("Go");
            b.fixed("i", "Insert (type)");
            b.fixed("h", "Explorer");
            b.fixed("gt / gT", "Next / previous tab");
            b.fixed(":", "Command line");
            b.fixed("Space", "Every action (leader)");
        }
        ("pane", false, false) => {
            b.group("Move");
            b.fixed("Up / Down", "Scroll");
            b.fixed("PgUp / PgDn", "A page");
            b.fixed("Home / End", "Top / latest");
            b.group("Act");
            b.action("link-hints", "Open a link");
            b.action("artifact-previous", "Cards");
            b.action("stop-agent", "Stop the agent");
            b.action("agent-processes", "Jobs and helpers");
            b.action("switcher", "Commands");
            b.group("Go");
            b.action("focus-composer", "Insert (type)");
            b.action("focus-sidebar", "Explorer");
            b.action("next-attention", "Next attention");
            b.action("split-right", "Split right");
            b.action("close-pane", "Close pane");
            b.action("new-workspace", "New tab");
            b.action("next-workspace", "Next tab");
        }
        ("sidebar", _, true) => {
            b.group("Move");
            b.fixed("j / k", "Next / previous chat");
            b.fixed("gg / G", "First / last");
            b.group("Act");
            b.fixed("l", "Open (or unfold)");
            b.fixed("Enter", "Open and type");
            b.fixed("/", "Filter");
            b.fixed("p", "Live preview");
            b.fixed("zo / zc", "Unfold / fold");
            b.action("agent-processes", "Jobs and helpers");
            b.group("Go");
            b.fixed("h", "Chat");
            b.fixed("i", "Insert (type)");
            b.fixed(":", "Command line");
            b.fixed("Space", "Every action (leader)");
        }
        ("sidebar", _, false) => {
            b.group("Move");
            b.action("agent-next", "Next chat");
            b.action("agent-previous", "Previous chat");
            b.group("Act");
            b.action("agent-open", "Open");
            b.action("agent-search", "Filter");
            b.action("toggle-preview", "Live preview");
            b.action("unfold", "Unfold");
            b.action("fold", "Fold");
            b.action("agent-processes", "Jobs and helpers");
            b.action("new", "New agent");
            b.group("Go");
            b.action("focus-pane", "Chat");
            b.action("focus-composer", "Insert (type)");
            b.action("sidebar", "Hide the explorer");
            b.action("switcher", "Commands");
        }
        (other, _, _) => {
            b.group("Here");
            for binding in keymap::resolve(other, overrides, None).into_iter().filter(|x| x.hint && x.action != "help-keys").take(10) {
                b.action(binding.action, binding.label);
            }
            b.group("Go");
            b.action("chats", "Chats");
            b.action("switcher", "Commands");
        }
    }
    // The user's own bindings that reach here.
    let mut yours = Vec::new();
    for (action, label) in keymap::actions() {
        let mine = keymap::user_keys(state, action, overrides);
        if !mine.is_empty() && yours.len() < YOURS {
            yours.push(Item { keys: mine.iter().take(2).map(|k| spell(k, vim)).collect::<Vec<_>>().join(" / "), label: label.to_owned() });
        }
    }
    if !yours.is_empty() {
        b.groups.push(Group { title: "Yours", items: yours });
    }
    b.groups.retain(|g| !g.items.is_empty());
    let editor = keys(state, "edit-keymap", overrides, false).unwrap_or_default();
    let commands = keymap::keys_in(state, "switcher", overrides).into_iter().find(|k| k.starts_with("Ctrl+")).map(|k| keymap::display(&k)).unwrap_or_default();
    let footer = if vim {
        format!("Everything else: Space k or {editor} (key bindings)  ·  Space Space or {commands} (commands)  ·  ? or Esc closes")
    } else {
        format!("Everything else: {editor} (key bindings)  ·  {commands} (commands)  ·  ? or Esc closes")
    };
    Help { title, groups: b.groups, footer }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn items(help: &Help) -> Vec<(String, String)> {
        help.groups.iter().flat_map(|g| g.items.iter().map(|i| (i.keys.clone(), i.label.clone()))).collect()
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
        assert_eq!(yours.items, [Item { keys: "Ctrl+J / Right ×2".into(), label: "Next agent".into() }]);
        // A rebound action shows the user's key first.
        let link = keymap::add(&Overrides::new(), "link-hints", "main", "Ctrl+Shift+L").unwrap();
        assert!(has(&help("pane", false, false, &link), "Ctrl+Shift+L / F", "Open a link"));
    }

    #[test]
    fn the_footer_says_where_everything_is() {
        let none = Overrides::new();
        let vim = help("pane", false, true, &none).footer;
        assert!(vim.contains("Space k") && vim.contains("Ctrl+K") && vim.contains("Ctrl+Alt+,"), "{vim}");
        let classic = help("sidebar", false, false, &none).footer;
        assert!(classic.contains("Ctrl+K (commands)") && classic.contains("Esc closes"), "{classic}");
    }
}
