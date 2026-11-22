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
        let path = crate::config::host_config_dir().join("config.toml");
        let table = crate::owner::OwnerFile::new(Some(path)).read();
        Self(table.and_then(|mut table| table.remove("conversation_forks")))
    }

    /// Whether this agent may fork: Claude Code, and named by its session or its id. What
    /// the apps are told per agent (`conversation_fork` in the snapshot).
    pub(crate) fn allows(&self, backend: &str, agent_id: &str, session: &str) -> bool {
        backend == "claude" && names(self.0.as_ref(), agent_id, session)
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

/// Whether a message of the user's to this agent may go to its fork: Claude Code, the chat
/// seen in Conversation view (Session view is the one session, and is never forked), and the
/// agent named in the owner's setting. Whether its own turn works for other agents is the
/// actor's to say ([`clarp_actor::Actor::fork_take`]).
pub(crate) fn may_fork(backend: &str, view: View, agent_id: &str, session: &str) -> bool {
    backend == "claude" && view == View::Conversation && named(agent_id, session)
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
    fn session_view_and_other_tools_never_fork() {
        // The setting is not read for these: nothing names them on a test machine either.
        assert!(!may_fork("codex", View::Conversation, "a-1", "x"));
        assert!(!may_fork("claude", View::Session, "a-1", "x"));
    }
}
