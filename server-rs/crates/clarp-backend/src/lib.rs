//! Backends: the tools that do an agent's work.
//!
//! A backend is two things and nothing else. It knows how to start its tool for one turn
//! ([`Backend::launch`]), and it knows what the lines that tool prints mean ([`Interpreter`]).
//! It never runs anything and never touches the server: the shim runs the tool and records what
//! it prints, and an interpreter is a fold over that record. Given the same records it gives the
//! same signals, however they are cut into reads and however often the server restarted in
//! between. That is what lets a server that comes back in the middle of a turn read the turn's
//! spool from its beginning and arrive exactly where the one before it was.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use clarp_kernel::conversation::{Notice, Reply, ToolCall};
use clarp_kernel::live::Glimpse;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::Record;

pub mod agy;
pub mod catalogue;
pub mod claude;
pub mod codex;
pub mod history;
pub mod past;
pub mod scripted;

/// The environment variable that tells a tool, its hooks and its skills which agent they work for.
/// The name is the Python Host's: every skill written for it reads this.
pub const SESSION_ENV: &str = "CLAUDE_PWA_SESSION";

/// What a backend needs to know to start one turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TurnSpec {
    pub agent_id: String,
    /// The agent's session name: what its tools know it by.
    pub session: String,
    pub turn: u64,
    pub cwd: PathBuf,
    /// The message this turn answers.
    pub prompt: String,
    /// The tool's own session to continue, when the agent has one.
    pub backend_session: Option<String>,
    /// The name the Host wants the tool to give a session it starts. A tool that can be told
    /// uses it, so that the conversation has its name before the tool has said a word.
    pub new_session: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    /// What the agent is told about who it is, on top of the tool's own instructions.
    pub identity: Option<String>,
    /// What the Host tells the tool with this prompt: to use the Host's own tools, whose
    /// turn this is and, for an answer that is read aloud, how to speak. `None`: nothing.
    pub told: Option<clarp_kernel::told::Turn>,
}

/// One question to a tool's model, outside every conversation: answered once, and nothing
/// of it kept by the tool. What a janitor that answers one request asks.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Question {
    pub prompt: String,
    pub model: Option<String>,
    pub effort: Option<String>,
}

/// Something a tool's model is to read and say something of, and must never act on: a
/// janitor that explains what agents do is shown what they did, which is whatever stood in
/// their work.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Reading {
    /// How to answer: for whom, how long.
    pub instructions: String,
    /// What is to be read.
    pub asked: String,
    /// The shape the answer has to have, as a JSON schema.
    pub schema: String,
    pub model: Option<String>,
    pub effort: Option<String>,
}

/// How to start a tool for a [`Reading`]: in a directory of its own that holds these files
/// and nothing else, with nothing to act with, and with no more of the Host's environment
/// than is named.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Apart {
    /// `{dir}` in an argument stands for the directory the tool is started in.
    pub launch: Launch,
    /// The files it is given there, each by its name.
    pub files: Vec<(String, String)>,
    /// The variables of the environment it is left with.
    pub keeps: Vec<&'static str>,
}

/// How to start the tool for one turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Launch {
    pub program: String,
    pub args: Vec<String>,
    /// Set on top of the environment the tool inherits.
    pub env: Vec<(String, String)>,
    /// What the tool reads on its standard input, which is then closed.
    pub stdin: Vec<u8>,
}

/// A reply while it is being written: shown to whoever is watching, never stored.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveReply {
    /// The id the finished reply will have, so a client replaces one with the other.
    pub id: String,
    pub text: String,
}

/// What a stretch of a tool's output means.
#[derive(Debug, Clone, PartialEq)]
pub enum Signal {
    /// The tool named its own session.
    Bound {
        backend_session: String,
    },
    /// The tool is doing something else now.
    Moved(Activity),
    /// A whole message of the agent's.
    Reply(Reply),
    /// A tool call started, or ended.
    Tool(ToolCall),
    /// Something the user should be told that the agent did not say.
    Notice(Notice),
    Live(LiveReply),
    /// Something of the turn as it runs, for those who watch it: a message or a thought as it
    /// stands, a tool call, what a tool returned. Shown and never stored
    /// (`docs/live-items.md`).
    Glimpse(Glimpse),
    /// The model the tool says it runs on.
    Model(String),
    /// What the tool says the turn took in and gave out, in tokens.
    Usage {
        tokens_in: u64,
        tokens_out: u64,
    },
    /// What the tool says its whole conversation has taken in and given out so far, where
    /// that is all it says at a turn's end (Codex: the count goes on from turn to turn of one
    /// thread). The turn's own share is the difference from what was said at the end of the
    /// turn before, in the same conversation.
    Total {
        tokens_in: u64,
        tokens_out: u64,
    },
    /// How many tokens the tool's conversation holds now, where the tool says so with an
    /// answer: how full it is.
    Context {
        tokens: u64,
    },
    /// What the tool says the turn cost, in millionths of a dollar.
    Cost {
        micros: u64,
    },
    /// The tool process is gone, and this is how the turn went.
    Ended(RunOutcome),
}

/// What the lines of one turn mean. One interpreter reads one turn's spool, from its first record.
pub trait Interpreter: Send {
    /// Takes the next record. What it means is added to `out`, in order.
    fn on_record(&mut self, record: &Record, out: &mut Vec<Signal>);
}

pub trait Backend: Send + Sync {
    /// The name this backend has in configuration and on the wire.
    fn id(&self) -> &'static str;

    fn launch(&self, spec: &TurnSpec) -> Launch;

    /// The interpreter of one turn's output. `name` is a name for the turn that no other turn
    /// of any agent on any Host has: what the interpreter builds the ids of its pieces from
    /// when the tool gives it none. (A turn's number is not such a name: an agent that moves
    /// to another Host starts counting again there.)
    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter>;

    /// How many tokens a conversation of this tool can hold, for a tool whose conversations
    /// fill up and are compacted where the user can see it. `None` for a tool that manages
    /// its context on its own: there is no gauge to show.
    fn context_window(&self) -> Option<u64> {
        None
    }

    /// How hard this tool can be told to think: the efforts an agent of it may be set to.
    /// Empty for a tool that takes none.
    fn efforts(&self) -> &'static [&'static str] {
        &[]
    }

    /// The file this tool keeps its login in, for a user whose home is `home`: the file
    /// that is another one after someone has put another account in place. `None` for a
    /// tool that has no login.
    fn login_file(&self, _home: &Path) -> Option<PathBuf> {
        None
    }

    /// How to start the tool for one question: it answers once and keeps no conversation
    /// of it. The question is read on standard input, never from the command line, which
    /// everybody on the machine can read. `None` for a tool that cannot be asked so.
    fn once(&self, _question: &Question) -> Option<Launch> {
        None
    }

    /// The answer in what the tool printed for one question. `Err`: why there is none.
    fn answered(&self, printed: &str) -> Result<String, String> {
        Ok(printed.to_string())
    }

    /// How to start the tool to read something it must not act on: with nothing to act
    /// with. `None` for a tool that has no such way; it is then asked as for any question
    /// ([`Backend::once`]), in a directory of its own.
    fn reads(&self, _reading: &Reading) -> Option<Apart> {
        None
    }
}

/// The environment variable that names Claude Code's program, when it is not `claude` on the path.
pub const CLAUDE_PROGRAM_ENV: &str = "CLARP_CLAUDE_PROGRAM";
/// The same for Codex.
pub const CODEX_PROGRAM_ENV: &str = "CLARP_CODEX_PROGRAM";

/// The program an environment variable names, when it names one.
fn named(variable: &str) -> Option<String> {
    std::env::var(variable)
        .ok()
        .filter(|program| !program.trim().is_empty())
}

/// The program a tool is started as: the one its environment variable names, else the
/// tool's own name, found on the path. `None` for what is no tool a person installs.
pub fn program(id: &str) -> Option<String> {
    match id {
        "claude" => Some(named(CLAUDE_PROGRAM_ENV).unwrap_or_else(|| "claude".to_string())),
        "codex" => Some(named(CODEX_PROGRAM_ENV).unwrap_or_else(|| "codex".to_string())),
        _ => None,
    }
}

/// The backend with this name, as built in.
pub fn by_id(id: &str) -> Option<Arc<dyn Backend>> {
    match id {
        "claude" => Some(Arc::new(match named(CLAUDE_PROGRAM_ENV) {
            Some(program) => claude::Claude::with_program(program),
            None => claude::Claude::new(),
        })),
        "codex" => Some(Arc::new(match named(CODEX_PROGRAM_ENV) {
            Some(program) => codex::Codex::with_program(program),
            None => codex::Codex::new(),
        })),
        "scripted" => Some(Arc::new(scripted::Scripted::echo())),
        _ => None,
    }
}

/// The context window of the backend with this name, when it declares one.
pub fn context_window(id: &str) -> Option<u64> {
    by_id(id).and_then(|backend| backend.context_window())
}

/// The efforts an agent of the backend with this name may be set to.
pub fn efforts(id: &str) -> &'static [&'static str] {
    by_id(id).map_or(&[], |backend| backend.efforts())
}

/// The first `max` characters of a text, with a mark when something was left out.
pub(crate) fn shorten(text: &str, max: usize) -> String {
    match text.char_indices().nth(max) {
        None => text.to_string(),
        Some((cut, _)) => format!("{}…", &text[..cut]),
    }
}

/// The last lines a tool wrote to its standard error: what there is to say when it fails.
#[derive(Debug, Default)]
pub(crate) struct ErrorTail {
    lines: std::collections::VecDeque<String>,
}

impl ErrorTail {
    const LINES: usize = 20;
    const CHARS: usize = 600;

    pub(crate) fn push(&mut self, line: &str) {
        if line.trim().is_empty() {
            return;
        }
        if self.lines.len() == Self::LINES {
            self.lines.pop_front();
        }
        self.lines.push_back(line.to_string());
    }

    pub(crate) fn text(&self) -> Option<String> {
        if self.lines.is_empty() {
            return None;
        }
        let joined = self.lines.iter().cloned().collect::<Vec<_>>().join("\n");
        Some(shorten(&joined, Self::CHARS))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_built_in_backends_are_found_by_name() {
        assert_eq!(by_id("claude").unwrap().id(), "claude");
        assert_eq!(by_id("codex").unwrap().id(), "codex");
        assert_eq!(
            (context_window("claude"), context_window("codex")),
            (Some(1_000_000), None),
            "Codex compacts on its own: there is no gauge to show"
        );
        assert_eq!(by_id("scripted").unwrap().id(), "scripted");
        assert!(by_id("nobody").is_none());
    }

    #[test]
    fn a_text_is_shortened_at_a_character_not_in_the_middle_of_one() {
        assert_eq!(shorten("hello", 5), "hello");
        assert_eq!(shorten("hello", 4), "hell…");
        assert_eq!(shorten("blåbær", 3), "blå…");
        assert_eq!(shorten("", 0), "");
    }

    #[test]
    fn the_error_tail_keeps_the_last_lines_only() {
        let mut tail = ErrorTail::default();
        assert_eq!(tail.text(), None);
        tail.push("   ");
        for n in 0..30 {
            tail.push(&format!("line {n}"));
        }
        let text = tail.text().unwrap();
        assert!(text.starts_with("line 10\n") && text.ends_with("line 29"));
    }
}
