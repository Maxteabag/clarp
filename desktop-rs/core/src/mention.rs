//! `@agent` mentions in the composer: the token being typed, the agents
//! that match it (fuzzy, best first), the text once one is chosen, and the
//! agent a message is addressed to. Offsets are bytes, as the editor's
//! cursor is.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Active {
    /// Where the `@` is.
    pub start: usize,
    /// Where the token ends (it may run on past the cursor).
    pub end: usize,
    /// What was typed between the `@` and the cursor.
    pub query: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub session: String,
    pub name: String,
    /// The agent's latest activity; more recent ranks first among equals.
    pub activity: i64,
}

pub fn active(_text: &str, _cursor: usize) -> Option<Active> {
    None
}

pub fn rank(_candidates: &[Candidate], _query: &str, _limit: usize) -> Vec<Candidate> {
    Vec::new()
}

pub fn complete(text: &str, _mention: &Active, _name: &str) -> (String, usize) {
    (text.to_owned(), 0)
}

pub fn target<'a>(_text: &str, _candidates: &'a [Candidate]) -> Option<&'a Candidate> {
    None
}
