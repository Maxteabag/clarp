//! Which agents may answer their user through a conversation fork (issue 718): the owner's
//! setting, read anew at each message it could apply to, as the experience watcher reads its
//! own. Off for every agent the setting does not name.
//!
//! ```toml
//! [conversation_forks]
//! agents = ["hazel-74f6"]
//! ```

use clarp_kernel::chat_view::View;

/// The agents the owner's setting names, read once for a whole answer.
pub(crate) struct Named(Option<toml::Value>);

impl Named {
    pub(crate) fn read() -> Self {
        let path = crate::config::host_config_file();
        let table = crate::owner::OwnerFile::new(path).read();
        Self(table.and_then(|mut table| table.remove("conversation_forks")))
    }

    /// Whether this agent may answer beside its work: named by its session or its id. What
    /// the apps are told per agent (`conversation_fork` in the snapshot).
    pub(crate) fn allows(&self, backend: &str, agent_id: &str, session: &str) -> bool {
        clarp_backend::has_conversation(backend) && names(self.0.as_ref(), agent_id, session)
    }
}

/// Whether the setting names this agent, by its session or its id.
pub(crate) fn named(agent_id: &str, session: &str) -> bool {
    names(Named::read().0.as_ref(), agent_id, session)
}

fn names(section: Option<&toml::Value>, agent_id: &str, session: &str) -> bool {
    section
        .and_then(|section| section.get("agents"))
        .and_then(toml::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(toml::Value::as_str)
        .map(str::trim)
        .any(|named| !named.is_empty() && (named == session || named == agent_id))
}

/// Whether a user's message may go to the conversation lane for a named agent. The actor
/// chooses between a native fork and a fresh conversation on that backend.
pub(crate) fn may_fork(backend: &str, view: View, agent_id: &str, session: &str) -> bool {
    clarp_backend::has_conversation(backend)
        && view == View::Conversation
        && named(agent_id, session)
}

/// Puts `extra` (Claude Code settings, JSON) into the turn's arguments: into the `--settings`
/// they have already, hook lists joined event by event, or as a `--settings` of its own. Claude
/// Code reads one `--settings`: a fork's turn carries its edit guard there, and the Host's own
/// hooks join it.
pub(crate) fn add_settings(args: &mut Vec<String>, extra: &str) {
    let at = args
        .iter()
        .position(|arg| arg == "--settings")
        .filter(|at| *at + 1 < args.len());
    let Some(at) = at else {
        args.extend(["--settings".to_owned(), extra.to_owned()]);
        return;
    };
    let parse = |text: &str| serde_json::from_str::<serde_json::Value>(text).ok();
    let (Some(mut had), Some(extra)) = (parse(&args[at + 1]), parse(extra)) else {
        return;
    };
    for (event, entries) in extra["hooks"].as_object().into_iter().flatten() {
        let Some(entries) = entries.as_array() else {
            continue;
        };
        let list = &mut had["hooks"][event];
        if !list.is_array() {
            *list = serde_json::Value::Array(Vec::new());
        }
        if let Some(list) = list.as_array_mut() {
            list.extend(entries.iter().cloned());
        }
    }
    args[at + 1] = had.to_string();
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn section(text: &str) -> toml::Value {
        let table: toml::Table = toml::from_str(text).unwrap();
        table["conversation_forks"].clone()
    }

    #[test]
    fn only_the_agents_the_setting_names_may_fork() {
        let set = section("[conversation_forks]\nagents = [\"hazel-74f6\", \" a-2 \"]\n");
        assert!(names(Some(&set), "a-1", "hazel-74f6"));
        assert!(names(Some(&set), "a-2", "other"));
        assert!(!names(Some(&set), "a-3", "nori-5aac"));
        assert!(!names(None, "a-1", "hazel-74f6"), "no setting: off");
        let empty = section("[conversation_forks]\nagents = [\"\"]\n");
        assert!(!names(Some(&empty), "", ""));
        let wrong = section("[conversation_forks]\nagents = \"hazel-74f6\"\n");
        assert!(
            !names(Some(&wrong), "a-1", "hazel-74f6"),
            "a list or nothing"
        );
    }

    #[test]
    fn session_view_and_unsupported_backends_never_fork() {
        // The setting is not read for these: nothing names them on a test machine either.
        assert!(!may_fork("grok", View::Conversation, "a-1", "x"));
        assert!(!may_fork("claude", View::Session, "a-1", "x"));
    }

    #[test]
    fn the_hosts_hooks_join_the_settings_a_fork_brings() {
        let mut args = vec![
            "-p".to_owned(),
            "--settings".to_owned(),
            clarp_actor::fork::edit_guard().to_string(),
        ];
        let host = r#"{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[]}]}}"#;
        add_settings(&mut args, host);
        assert_eq!(args.iter().filter(|arg| *arg == "--settings").count(), 1);
        let merged: serde_json::Value = serde_json::from_str(&args[2]).unwrap();
        let matchers: Vec<&str> = merged["hooks"]["PreToolUse"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| entry["matcher"].as_str().unwrap())
            .collect();
        assert_eq!(matchers, [clarp_actor::fork::EDIT_TOOLS, "Bash"]);
        let mut plain = vec!["-p".to_owned()];
        add_settings(&mut plain, host);
        assert_eq!(plain, ["-p", "--settings", host]);
    }
}
