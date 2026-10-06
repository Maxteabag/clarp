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

/// The mention the cursor is in: an `@` at the start of a word, then no
/// whitespace up to the cursor.
pub fn active(text: &str, cursor: usize) -> Option<Active> {
    if cursor > text.len() || !text.is_char_boundary(cursor) {
        return None;
    }
    let before = &text[..cursor];
    let start = before.rfind(|c: char| c == '@' || c.is_whitespace())?;
    if !before[start..].starts_with('@') {
        return None;
    }
    if before[..start].chars().next_back().is_some_and(|c| !c.is_whitespace() && !"([{\"'".contains(c)) {
        return None;
    }
    let end = cursor + text[cursor..].find(char::is_whitespace).unwrap_or(text.len() - cursor);
    Some(Active { start, end, query: before[start + 1..].to_owned() })
}

fn subsequence(haystack: &str, needle: &str) -> bool {
    let mut letters = haystack.chars();
    needle.chars().all(|n| letters.any(|h| h == n))
}

/// How `query` matches `field` (lower case): exact 0, prefix 1, a word's
/// start or the initials 2, inside 3, scattered 4.
fn tier(field: &str, query: &str) -> Option<u8> {
    let field = field.to_lowercase();
    if field == query {
        return Some(0);
    }
    if field.starts_with(query) {
        return Some(1);
    }
    let words: Vec<&str> = field.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).collect();
    let initials: String = words.iter().filter_map(|w| w.chars().next()).collect();
    if words.iter().skip(1).any(|w| w.starts_with(query)) || initials.starts_with(query) {
        return Some(2);
    }
    if field.contains(query) {
        return Some(3);
    }
    subsequence(&field, query).then_some(4)
}

/// The agents matching `query` by name or session, best first, then the
/// most recently active; at most `limit`.
pub fn rank(candidates: &[Candidate], query: &str, limit: usize) -> Vec<Candidate> {
    let query = query.to_lowercase();
    let mut matched: Vec<(u8, &Candidate)> = candidates
        .iter()
        .filter_map(|c| {
            if query.is_empty() {
                return Some((0, c));
            }
            [tier(&c.name, &query), tier(&c.session, &query)].into_iter().flatten().min().map(|t| (t, c))
        })
        .collect();
    matched.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.activity.cmp(&a.1.activity)).then(a.1.name.cmp(&b.1.name)));
    matched.into_iter().take(limit).map(|(_, c)| c.clone()).collect()
}

/// `text` with the mention replaced by `@name` and a space, and where the
/// cursor goes (after the space).
pub fn complete(text: &str, mention: &Active, name: &str) -> (String, usize) {
    let rest = &text[mention.end.min(text.len())..];
    let space = if rest.starts_with(char::is_whitespace) { "" } else { " " };
    let completed = format!("{}@{name}{space}{rest}", &text[..mention.start]);
    let cursor = mention.start + 1 + name.len() + 1;
    (completed, cursor)
}

/// The agent the first `@name` of `text` names (any case; the longest name
/// when several fit), when it ends at a word boundary.
pub fn target<'a>(text: &str, candidates: &'a [Candidate]) -> Option<&'a Candidate> {
    let mut sorted: Vec<&Candidate> = candidates.iter().filter(|c| !c.name.is_empty()).collect();
    sorted.sort_by_key(|c| std::cmp::Reverse(c.name.len()));
    for (at, _) in text.match_indices('@') {
        if text[..at].chars().next_back().is_some_and(|c| !c.is_whitespace() && !"([{\"'".contains(c)) {
            continue;
        }
        let after = &text[at + 1..];
        let found = sorted.iter().find(|c| {
            let Some(head) = after.get(..c.name.len()) else { return false };
            head.to_lowercase() == c.name.to_lowercase() && after[c.name.len()..].chars().next().is_none_or(|n| !n.is_alphanumeric())
        });
        if let Some(candidate) = found {
            return Some(candidate);
        }
    }
    None
}
