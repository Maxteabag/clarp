//! A turn's helpers as one card in the chat, never as a line of system text:
//! "2 helpers working · 5 s" while they run (the count, and the time since the
//! first started, ticking), then "2 helpers finished" with one folded line per
//! helper, its result. A helper with a chat of its own (a Clarp helper agent)
//! opens to that chat; a helper inside the agent's own process (the sub-agent
//! cells the Host sends, `kind: "subagents"`) opens to its detail lines.

use serde_json::Value;

use crate::json::{self, Object};
use crate::protocol::{Agent, describe_subagent_cell, display_name};

/// Running, done or failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum State {
    #[default]
    Running,
    Done,
    Failed,
}

impl State {
    pub fn word(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Done => "done",
            Self::Failed => "failed",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Helper {
    pub key: String,
    pub name: String,
    /// Its one folded line: the task while it runs, the result once done.
    pub line: String,
    pub state: State,
    /// The helper agent's session, whose chat it opens to; empty for a
    /// helper inside the agent's own process.
    pub chat: String,
    /// The detail lines it opens to when it has no chat: (kind, text).
    pub lines: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Card {
    pub key: String,
    pub helpers: Vec<Helper>,
    /// When the first helper started (epoch ms); 0 when unknown.
    pub started_ms: i64,
}

impl Card {
    pub fn running(&self) -> usize {
        self.helpers.iter().filter(|h| h.state == State::Running).count()
    }

    pub fn failed(&self) -> usize {
        self.helpers.iter().filter(|h| h.state == State::Failed).count()
    }

    /// The card's title `elapsed` seconds after its first helper started.
    pub fn title(&self, elapsed: i64) -> String {
        title(self.running(), self.helpers.len(), self.failed(), elapsed)
    }
}

/// "2 helpers working · 5 s" while any runs, else "2 helpers finished" and
/// how many failed ("· 1 failed"). `elapsed` below zero leaves the time out.
pub fn title(running: usize, total: usize, failed: usize, elapsed: i64) -> String {
    let plural = |n: usize| if n == 1 { "helper" } else { "helpers" };
    if running > 0 {
        let mut line = format!("{running} {} working", plural(running));
        if elapsed >= 0 {
            line.push_str(" · ");
            line.push_str(&duration(elapsed));
        }
        return line;
    }
    let mut line = format!("{total} {} finished", plural(total));
    if failed > 0 {
        line.push_str(&format!(" · {failed} failed"));
    }
    line
}

/// "5 s", "2 min 5 s", "1 h 4 min".
pub fn duration(seconds: i64) -> String {
    let seconds = seconds.max(0);
    match seconds {
        0..60 => format!("{seconds} s"),
        60..3600 if seconds % 60 == 0 => format!("{} min", seconds / 60),
        60..3600 => format!("{} min {} s", seconds / 60, seconds % 60),
        _ => format!("{} h {} min", seconds / 3600, seconds % 3600 / 60),
    }
}

/// The helpers among a row's display cells, as one card keyed by `key`;
/// none when the row started no helper. `started_ms` is when the row was
/// written. Several cells about one helper (Codex sends one per spawn,
/// wait and close) are one helper, its latest cell's state.
pub fn from_cells(key: &str, cells: &[Value], started_ms: i64) -> Option<Card> {
    let mut helpers: Vec<Helper> = Vec::new();
    for cell in cells.iter().filter_map(Value::as_object) {
        let shown = describe_subagent_cell(cell);
        if shown.is_empty() {
            continue;
        }
        let name = json::string(&shown, "name");
        let phase = json::string(&shown, "phase");
        let status = json::string(cell, "status");
        let state = if phase == "failed" || status == "error" {
            State::Failed
        } else if status == "running" {
            State::Running
        } else {
            State::Done
        };
        let lines: Vec<(String, String)> = json::array(cell, "lines")
            .iter()
            .filter_map(Value::as_object)
            .map(|line| {
                let (label, text) = (json::string(line, "label"), json::string(line, "text"));
                (json::string(line, "kind"), if label.is_empty() { text } else { format!("{label}  {text}") })
            })
            .filter(|(_, text)| !text.trim().is_empty())
            .collect();
        let task = json::string(&shown, "task");
        let result = result_line(cell);
        let line = match state {
            State::Running => task,
            _ if !result.is_empty() => result,
            _ => task,
        };
        let helper = Helper { key: format!("{key}:{}", json::string(cell, "id")), name: name.clone(), line, state, chat: String::new(), lines };
        match helpers.iter_mut().find(|h| h.name == name) {
            Some(known) => {
                let key = std::mem::take(&mut known.key);
                *known = Helper { key, line: if helper.line.is_empty() { known.line.clone() } else { helper.line }, ..helper };
            }
            None => helpers.push(helper),
        }
    }
    (!helpers.is_empty()).then(|| Card { key: key.to_owned(), helpers, started_ms })
}

/// A finished cell's result in one line: its `Result` (or `Status`) line.
fn result_line(cell: &Object) -> String {
    let lines = json::array(cell, "lines");
    let find = |wanted: &str| {
        lines.iter().filter_map(Value::as_object).find(|l| json::string(l, "label") == wanted).map(|l| json::string(l, "text").trim().to_owned())
    };
    let text = find("Result").or_else(|| find("Status")).unwrap_or_default();
    crate::agent_prompt::first_line(&text)
}

/// The helper agents of `parent` still at work, as one card: each one's
/// name, its own status line, and its chat. None when none runs.
pub fn from_agents(key: &str, parent: &Agent, agents: &[Agent]) -> Option<Card> {
    let helpers: Vec<&Agent> =
        agents.iter().filter(|a| a.is_helper() && !a.archived && a.parent_agent_id == parent.agent_id && a.helper_running()).collect();
    if helpers.is_empty() {
        return None;
    }
    let started_ms = helpers.iter().filter_map(|a| a.turn_started_at).filter(|t| *t > 0).min().unwrap_or(0);
    let helpers = helpers
        .into_iter()
        .map(|agent| Helper {
            key: format!("{key}:{}", agent.session),
            name: display_name(agent).to_owned(),
            line: crate::agent_prompt::first_line(&agent.status_text),
            state: State::Running,
            chat: agent.session.clone(),
            lines: Vec::new(),
        })
        .collect();
    Some(Card { key: key.to_owned(), helpers, started_ms })
}

/// The helper agent of `parent` whose report a row from another agent is
/// (`sender_agent_id` names one of its helpers): that helper's session.
pub fn reporting_helper<'a>(sender_agent_id: &str, parent: &Agent, agents: &'a [Agent]) -> Option<&'a Agent> {
    if sender_agent_id.trim().is_empty() {
        return None;
    }
    agents.iter().find(|a| a.agent_id == sender_agent_id && a.is_helper() && a.parent_agent_id == parent.agent_id)
}
