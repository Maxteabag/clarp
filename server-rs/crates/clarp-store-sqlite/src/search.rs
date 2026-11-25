//! The message search index (issue 803): what agents' conversations said, for `GET /search`.
//!
//! A file of its own beside the fleet's (`search.db`), owned by one thread of the Host: nothing
//! an agent writes waits for it. It is derived from the agents' logs and can be thrown away; the
//! Host then builds it again. A row's words and how far its agent has been read are written in
//! one transaction, so after a crash the index stands where its agent's mark says it does.
//! Hence `synchronous=NORMAL`: what a power cut takes back, the next read gives again.
//!
//! The words are kept in an FTS5 table, folded to lower case and without accents
//! (`unicode61 remove_diacritics 2`: "på" is found by "pa" as by "på"; ø and æ are letters of
//! their own and stay). Every word asked for must be in a row, each as the start of a word, so
//! "møte" finds "møtet" and "Oslo" finds "Oslofjorden".

use std::collections::HashMap;
use std::fmt;
use std::path::Path;

use rusqlite::{Connection, OptionalExtension, params};

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS hit (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    role TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    UNIQUE (agent_id, row_id)
) STRICT;
CREATE INDEX IF NOT EXISTS hit_time ON hit(ts_ms, id);
CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3'
);
CREATE TABLE IF NOT EXISTS agent (
    agent_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    begins_after INTEGER NOT NULL,
    older_than TEXT,
    complete INTEGER NOT NULL
) STRICT;
CREATE TEMP TABLE IF NOT EXISTS allowed (agent_id TEXT PRIMARY KEY);
";

/// What marks the start and the end of a match in a snippet as SQLite writes it. Taken out of
/// every text before it is kept, so a text cannot forge a mark.
const OPEN: char = '\u{1}';
const CLOSE: char = '\u{2}';
/// How many words a snippet shows around what matched.
const SNIPPET_WORDS: i64 = 16;
/// The most hits one page has.
pub const MOST: usize = 100;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    /// What was asked cannot be searched: no word in it, or a cursor this index never gave.
    Asked(String),
    /// The file underneath failed. The text is for the operator.
    Storage(String),
}

impl fmt::Display for SearchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Asked(why) => write!(f, "{why}"),
            Self::Storage(why) => write!(f, "search index failed: {why}"),
        }
    }
}

impl std::error::Error for SearchError {}

impl From<rusqlite::Error> for SearchError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Storage(error.to_string())
    }
}

/// A row of a conversation as it is kept: the id `/log` gives it, who said it, when, the
/// revision its words are of, and the words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub row_id: String,
    pub role: String,
    pub ts_ms: i64,
    pub revision: u64,
    pub text: String,
}

/// How far one agent's conversation has been read into the index.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Mark {
    /// Every change up to this revision is in the index: what is newer is read after it.
    pub revision: u64,
    /// Where the conversation began when it was read: a later beginning means it started
    /// again, and what was kept of it before is gone.
    pub begins_after: u64,
    /// The oldest row read so far, from which its older history is read; `None` before the
    /// first page.
    pub older_than: Option<String>,
    /// Its whole history is in the index.
    pub complete: bool,
}

/// One row that matched, its snippet in parts: `(text, matched)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hit {
    pub agent_id: String,
    pub row_id: String,
    pub role: String,
    pub ts_ms: i64,
    pub revision: u64,
    pub parts: Vec<(String, bool)>,
}

/// A page of hits, and the cursor of the next where there are more.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Found {
    pub hits: Vec<Hit>,
    pub next: Option<String>,
    /// One of the agents searched is not wholly in the index yet: what is older than what was
    /// read of it may be missing.
    pub partial: bool,
}

/// What a search asks.
#[derive(Debug, Clone, Copy)]
pub struct Asked<'a> {
    /// The words as the user typed them.
    pub words: &'a str,
    /// The agents whose rows may be found: the reader's, now. None is nothing found.
    pub agents: &'a [String],
    pub limit: usize,
    /// The cursor an earlier page gave.
    pub before: Option<&'a str>,
}

pub struct SearchIndex {
    db: Connection,
}

impl SearchIndex {
    /// Opens the index at `path`, creating it when it is not there.
    pub fn open(path: &Path) -> Result<Self, SearchError> {
        let db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "NORMAL")?;
        db.execute_batch(SCHEMA)?;
        private(path);
        Ok(Self { db })
    }

    /// Every agent the index holds anything of, with its mark.
    pub fn marks(&self) -> Result<HashMap<String, Mark>, SearchError> {
        let mut statement = self
            .db
            .prepare("SELECT agent_id, revision, begins_after, older_than, complete FROM agent")?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                Mark {
                    revision: unsigned(row.get(1)?),
                    begins_after: unsigned(row.get(2)?),
                    older_than: row.get(3)?,
                    complete: row.get::<_, i64>(4)? != 0,
                },
            ))
        })?;
        Ok(rows.collect::<Result<_, _>>()?)
    }

    /// Keeps these rows of an agent, as newer versions of what it held of them, and its new
    /// mark, together. A row whose words are gone leaves the index; an older version than the
    /// one kept changes nothing.
    pub fn apply(&mut self, agent_id: &str, rows: &[Row], mark: &Mark) -> Result<(), SearchError> {
        let tx = self.db.transaction()?;
        for row in rows {
            let held: Option<(i64, i64)> = tx
                .query_row(
                    "SELECT id, revision FROM hit WHERE agent_id = ?1 AND row_id = ?2",
                    params![agent_id, row.row_id],
                    |found| Ok((found.get(0)?, found.get(1)?)),
                )
                .optional()?;
            if held.is_some_and(|(_, revision)| unsigned(revision) > row.revision) {
                continue;
            }
            let text = kept_text(&row.text);
            if let Some((id, _)) = held {
                tx.execute("DELETE FROM words WHERE rowid = ?1", [id])?;
                if text.trim().is_empty() {
                    tx.execute("DELETE FROM hit WHERE id = ?1", [id])?;
                    continue;
                }
                tx.execute(
                    "UPDATE hit SET role = ?2, ts_ms = ?3, revision = ?4 WHERE id = ?1",
                    params![id, row.role, row.ts_ms, signed(row.revision)],
                )?;
                tx.execute(
                    "INSERT INTO words(rowid, text) VALUES (?1, ?2)",
                    params![id, text],
                )?;
            } else if !text.trim().is_empty() {
                tx.execute(
                    "INSERT INTO hit(agent_id, row_id, role, ts_ms, revision) \
                     VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![
                        agent_id,
                        row.row_id,
                        row.role,
                        row.ts_ms,
                        signed(row.revision)
                    ],
                )?;
                let id = tx.last_insert_rowid();
                tx.execute(
                    "INSERT INTO words(rowid, text) VALUES (?1, ?2)",
                    params![id, text],
                )?;
            }
        }
        tx.execute(
            "INSERT INTO agent(agent_id, revision, begins_after, older_than, complete) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(agent_id) DO UPDATE SET revision = ?2, begins_after = ?3, \
             older_than = ?4, complete = ?5",
            params![
                agent_id,
                signed(mark.revision),
                signed(mark.begins_after),
                mark.older_than,
                i64::from(mark.complete)
            ],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// Takes everything of an agent out of the index: it was removed, moved away, or its
    /// conversation began again.
    pub fn forget(&mut self, agent_id: &str) -> Result<(), SearchError> {
        let tx = self.db.transaction()?;
        tx.execute(
            "DELETE FROM words WHERE rowid IN (SELECT id FROM hit WHERE agent_id = ?1)",
            [agent_id],
        )?;
        tx.execute("DELETE FROM hit WHERE agent_id = ?1", [agent_id])?;
        tx.execute("DELETE FROM agent WHERE agent_id = ?1", [agent_id])?;
        tx.commit()?;
        Ok(())
    }

    /// The rows of these agents that have every word asked, newest first.
    pub fn search(&mut self, asked: Asked<'_>) -> Result<Found, SearchError> {
        let query = fts_query(asked.words)
            .ok_or_else(|| SearchError::Asked("nothing to search for in q".to_string()))?;
        let before = asked.before.map(cursor).transpose()?;
        let limit = asked.limit.clamp(1, MOST);
        let tx = self.db.transaction()?;
        tx.execute("DELETE FROM temp.allowed", [])?;
        {
            let mut allow = tx.prepare("INSERT OR IGNORE INTO temp.allowed VALUES (?1)")?;
            for agent in asked.agents {
                allow.execute([agent])?;
            }
        }
        let mut hits = Vec::new();
        {
            let mut statement = tx.prepare(
                "SELECT h.id, h.agent_id, h.row_id, h.role, h.ts_ms, h.revision, \
                 snippet(words, 0, char(1), char(2), '…', ?2) \
                 FROM words JOIN hit h ON h.id = words.rowid \
                 WHERE words MATCH ?1 AND h.agent_id IN (SELECT agent_id FROM temp.allowed) \
                 AND (?3 IS NULL OR h.ts_ms < ?3 OR (h.ts_ms = ?3 AND h.id < ?4)) \
                 ORDER BY h.ts_ms DESC, h.id DESC LIMIT ?5",
            )?;
            let (before_ts, before_id) = before.unzip();
            let rows = statement.query_map(
                params![
                    query,
                    SNIPPET_WORDS,
                    before_ts,
                    before_id,
                    i64::try_from(limit + 1).unwrap_or(i64::MAX)
                ],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        Hit {
                            agent_id: row.get(1)?,
                            row_id: row.get(2)?,
                            role: row.get(3)?,
                            ts_ms: row.get(4)?,
                            revision: unsigned(row.get(5)?),
                            parts: parts(&row.get::<_, String>(6)?),
                        },
                    ))
                },
            )?;
            for row in rows {
                hits.push(row?);
            }
        }
        let behind: i64 = tx.query_row(
            "SELECT count(*) FROM temp.allowed a LEFT JOIN agent m ON m.agent_id = a.agent_id \
             WHERE m.complete IS NOT 1",
            [],
            |row| row.get(0),
        )?;
        tx.execute("DELETE FROM temp.allowed", [])?;
        tx.commit()?;
        let more = hits.len() > limit;
        hits.truncate(limit);
        let next = more
            .then(|| hits.last().map(|(id, hit)| format!("{}.{id}", hit.ts_ms)))
            .flatten();
        Ok(Found {
            hits: hits.into_iter().map(|(_, hit)| hit).collect(),
            next,
            partial: behind > 0,
        })
    }
}

/// The index holds what agents said: only its owner reads the file.
fn private(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

fn unsigned(value: i64) -> u64 {
    u64::try_from(value).unwrap_or(0)
}

fn signed(value: u64) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

/// A text as it is kept: without the signs that mark a match.
fn kept_text(text: &str) -> String {
    text.replace([OPEN, CLOSE], " ")
}

/// The words typed, as an FTS5 query: each word a quoted term, every one the start of a word
/// of the row, all of them required. What has no letter or digit is no word. `None` where
/// nothing is left.
pub fn fts_query(words: &str) -> Option<String> {
    let terms: Vec<String> = words
        .split_whitespace()
        .map(|word| word.replace('"', ""))
        .filter(|word| word.chars().any(char::is_alphanumeric))
        .map(|word| format!("\"{word}\"*"))
        .collect();
    (!terms.is_empty()).then(|| terms.join(" "))
}

/// The cursor of a page: the time and the id of the last hit on the page before it.
fn cursor(text: &str) -> Result<(i64, i64), SearchError> {
    let unknown = || SearchError::Asked("unknown search cursor".to_string());
    let (ts, id) = text.split_once('.').ok_or_else(unknown)?;
    Ok((
        ts.parse().map_err(|_| unknown())?,
        id.parse().map_err(|_| unknown())?,
    ))
}

/// A snippet as SQLite marks it, in parts: what matched and what stands between.
fn parts(snippet: &str) -> Vec<(String, bool)> {
    let mut parts: Vec<(String, bool)> = Vec::new();
    let mut matched = false;
    let mut part = String::new();
    for sign in snippet.chars() {
        let flip = match sign {
            OPEN => Some(true),
            CLOSE => Some(false),
            _ => None,
        };
        match flip {
            Some(now) => {
                if !part.is_empty() {
                    parts.push((std::mem::take(&mut part), matched));
                }
                matched = now;
            }
            None => part.push(sign),
        }
    }
    if !part.is_empty() {
        parts.push((part, matched));
    }
    parts
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    struct Scratch(std::path::PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn index() -> (SearchIndex, Scratch) {
        let unique = format!(
            "clarp-search-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        let index = SearchIndex::open(&dir.join("search.db")).unwrap();
        (index, Scratch(dir))
    }

    fn row(id: &str, ts_ms: i64, revision: u64, text: &str) -> Row {
        Row {
            row_id: id.to_string(),
            role: "user".to_string(),
            ts_ms,
            revision,
            text: text.to_string(),
        }
    }

    fn mark(revision: u64) -> Mark {
        Mark {
            revision,
            ..Mark::default()
        }
    }

    fn agents(names: &[&str]) -> Vec<String> {
        names.iter().map(|name| name.to_string()).collect()
    }

    fn ids(index: &mut SearchIndex, words: &str, allowed: &[&str]) -> Vec<String> {
        let allowed = agents(allowed);
        index
            .search(Asked {
                words,
                agents: &allowed,
                limit: 50,
                before: None,
            })
            .unwrap()
            .hits
            .into_iter()
            .map(|hit| hit.row_id)
            .collect()
    }

    #[test]
    fn a_row_is_found_by_its_words_newest_first_and_marked() {
        let (mut index, _scratch) = index();
        let rows = [
            row("m1", 1_000, 1, "Shall we meet in Oslo on Friday?"),
            row("m2", 2_000, 2, "The train to Bergen leaves at nine."),
            row("m3", 3_000, 3, "Oslo it is, then."),
        ];
        index.apply("a1", &rows, &mark(3)).unwrap();
        assert_eq!(ids(&mut index, "oslo", &["a1"]), ["m3", "m1"]);
        assert_eq!(ids(&mut index, "Oslo friday", &["a1"]), ["m1"]);
        assert!(ids(&mut index, "stockholm", &["a1"]).is_empty());
        let allowed = agents(&["a1"]);
        let found = index
            .search(Asked {
                words: "bergen",
                agents: &allowed,
                limit: 10,
                before: None,
            })
            .unwrap();
        let hit = &found.hits[0];
        assert!(hit.parts.contains(&("Bergen".to_string(), true)));
        assert!(hit.parts.iter().any(|(text, on)| !on && text.contains("train")));
        assert_eq!((hit.agent_id.as_str(), hit.ts_ms, hit.revision), ("a1", 2_000, 2));
    }

    #[test]
    fn norwegian_words_are_found_with_and_without_their_accents_and_by_their_start() {
        let (mut index, _scratch) = index();
        let rows = [
            row("n1", 1, 1, "Vi tar møtet på kontoret i Trondheim."),
            row("n2", 2, 2, "Blåbærsyltetøy står i kjøleskapet."),
            row("n3", 3, 3, "Café au lait, s'il vous plaît."),
        ];
        index.apply("a1", &rows, &mark(3)).unwrap();
        assert_eq!(ids(&mut index, "møte", &["a1"]), ["n1"]);
        assert_eq!(ids(&mut index, "pa kontoret", &["a1"]), ["n1"]);
        assert_eq!(ids(&mut index, "på", &["a1"]), ["n1"]);
        assert_eq!(ids(&mut index, "blåbær", &["a1"]), ["n2"]);
        assert_eq!(ids(&mut index, "KJØLESKAPET", &["a1"]), ["n2"]);
        assert_eq!(ids(&mut index, "cafe", &["a1"]), ["n3"]);
        assert_eq!(ids(&mut index, "trondheim", &["a1"]), ["n1"]);
    }

    #[test]
    fn a_newer_revision_replaces_the_words_and_an_older_one_changes_nothing() {
        let (mut index, _scratch) = index();
        index
            .apply("a1", &[row("r1", 1, 5, "first draft about apples")], &mark(5))
            .unwrap();
        index
            .apply("a1", &[row("r1", 1, 9, "final answer about pears")], &mark(9))
            .unwrap();
        assert!(ids(&mut index, "apples", &["a1"]).is_empty());
        assert_eq!(ids(&mut index, "pears", &["a1"]), ["r1"]);
        // A page read again from before (a backfill behind a live update) is older: kept out.
        index
            .apply("a1", &[row("r1", 1, 5, "first draft about apples")], &mark(9))
            .unwrap();
        assert!(ids(&mut index, "apples", &["a1"]).is_empty());
        assert_eq!(index.marks().unwrap()["a1"].revision, 9);
    }

    #[test]
    fn a_row_whose_words_are_gone_leaves_the_index() {
        let (mut index, _scratch) = index();
        index
            .apply("a1", &[row("w1", 1, 1, "a message about kites")], &mark(1))
            .unwrap();
        index.apply("a1", &[row("w1", 1, 2, "  ")], &mark(2)).unwrap();
        assert!(ids(&mut index, "kites", &["a1"]).is_empty());
        // And an empty row is never kept at all.
        index.apply("a1", &[row("w2", 2, 3, "")], &mark(3)).unwrap();
        let held: i64 = index
            .db
            .query_row("SELECT count(*) FROM hit", [], |row| row.get(0))
            .unwrap();
        assert_eq!(held, 0);
    }

    #[test]
    fn a_forgotten_agent_is_never_found_again_and_others_stay() {
        let (mut index, _scratch) = index();
        index
            .apply("gone", &[row("g1", 1, 1, "secret plans for lunch")], &mark(1))
            .unwrap();
        index
            .apply("kept", &[row("k1", 2, 1, "public plans for lunch")], &mark(1))
            .unwrap();
        index.forget("gone").unwrap();
        assert_eq!(ids(&mut index, "lunch", &["gone", "kept"]), ["k1"]);
        assert!(!index.marks().unwrap().contains_key("gone"));
        let words: i64 = index
            .db
            .query_row("SELECT count(*) FROM words WHERE words MATCH 'secret'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(words, 0, "its words are gone from the full-text table too");
    }

    #[test]
    fn only_the_agents_asked_for_are_searched() {
        let (mut index, _scratch) = index();
        index
            .apply("open", &[row("o1", 1, 1, "the harbour at dawn")], &mark(1))
            .unwrap();
        index
            .apply("bound", &[row("b1", 2, 1, "the harbour at dusk")], &mark(1))
            .unwrap();
        assert_eq!(ids(&mut index, "harbour", &["open"]), ["o1"]);
        assert!(ids(&mut index, "harbour", &[]).is_empty());
        assert_eq!(ids(&mut index, "harbour", &["open", "bound"]), ["b1", "o1"]);
    }

    #[test]
    fn pages_follow_one_another_without_a_gap_or_a_repeat() {
        let (mut index, _scratch) = index();
        // Two rows share a time: the id orders them.
        let rows: Vec<Row> = (0..7)
            .map(|n| row(&format!("p{n}"), 1_000 + (n / 2), n as u64 + 1, "page me"))
            .collect();
        index.apply("a1", &rows, &mark(7)).unwrap();
        let allowed = agents(&["a1"]);
        let mut seen = Vec::new();
        let mut before: Option<String> = None;
        loop {
            let found = index
                .search(Asked {
                    words: "page",
                    agents: &allowed,
                    limit: 3,
                    before: before.as_deref(),
                })
                .unwrap();
            assert!(found.hits.len() <= 3);
            seen.extend(found.hits.into_iter().map(|hit| hit.row_id));
            match found.next {
                Some(next) => before = Some(next),
                None => break,
            }
        }
        assert_eq!(seen, ["p6", "p5", "p4", "p3", "p2", "p1", "p0"]);
    }

    #[test]
    fn nothing_to_search_for_and_a_strange_cursor_are_refused_in_words() {
        let (mut index, _scratch) = index();
        let allowed = agents(&["a1"]);
        let asked = |words, before| Asked {
            words,
            agents: &allowed,
            limit: 5,
            before,
        };
        assert!(matches!(index.search(asked("  \"?! ", None)), Err(SearchError::Asked(_))));
        assert!(matches!(
            index.search(asked("oslo", Some("yesterday"))),
            Err(SearchError::Asked(_))
        ));
    }

    #[test]
    fn what_is_typed_cannot_break_out_of_its_terms() {
        assert_eq!(fts_query("oslo"), Some("\"oslo\"*".to_string()));
        assert_eq!(
            fts_query("a\"b OR NOT c*"),
            Some("\"ab\"* \"OR\"* \"NOT\"* \"c*\"*".to_string())
        );
        let (mut index, _scratch) = index();
        index
            .apply("a1", &[row("x1", 1, 1, "NEAR(one two) or not")], &mark(1))
            .unwrap();
        // Operators of FTS5 typed by a user are words like any other.
        assert_eq!(ids(&mut index, "NEAR( or not", &["a1"]), ["x1"]);
        assert_eq!(ids(&mut index, "\" OR \"", &["a1"]), ["x1"]);
    }

    #[test]
    fn a_text_cannot_forge_a_mark() {
        let (mut index, _scratch) = index();
        let forged = format!("look {OPEN}here{CLOSE} at the comet");
        index.apply("a1", &[row("f1", 1, 1, &forged)], &mark(1)).unwrap();
        let allowed = agents(&["a1"]);
        let found = index
            .search(Asked {
                words: "comet",
                agents: &allowed,
                limit: 5,
                before: None,
            })
            .unwrap();
        let marked: Vec<&str> = found.hits[0]
            .parts
            .iter()
            .filter(|(_, on)| *on)
            .map(|(text, _)| text.as_str())
            .collect();
        assert_eq!(marked, ["comet"]);
    }

    #[test]
    fn a_search_says_partial_while_an_agent_asked_for_is_not_wholly_read() {
        let (mut index, _scratch) = index();
        let partial = |index: &mut SearchIndex, allowed: &[&str]| {
            let allowed = agents(allowed);
            index
                .search(Asked {
                    words: "tea",
                    agents: &allowed,
                    limit: 5,
                    before: None,
                })
                .unwrap()
                .partial
        };
        // Never read at all.
        assert!(partial(&mut index, &["a1"]));
        let begun = Mark {
            revision: 1,
            older_than: Some("t1".to_string()),
            ..Mark::default()
        };
        index.apply("a1", &[row("t1", 1, 1, "tea")], &begun).unwrap();
        assert!(partial(&mut index, &["a1"]));
        let done = Mark {
            complete: true,
            ..begun
        };
        index.apply("a1", &[], &done).unwrap();
        assert!(!partial(&mut index, &["a1"]));
        assert!(partial(&mut index, &["a1", "a2"]));
        assert!(!partial(&mut index, &[]));
    }

    #[test]
    fn marks_are_kept_with_the_rows_and_read_back() {
        let (mut index, _scratch) = index();
        let kept = Mark {
            revision: 42 << 20,
            begins_after: 3 << 20,
            older_than: Some("m9".to_string()),
            complete: false,
        };
        index.apply("a1", &[], &kept).unwrap();
        assert_eq!(index.marks().unwrap()["a1"], kept);
    }

    #[test]
    fn snippets_split_into_parts() {
        assert_eq!(
            parts("…to \u{1}Oslo\u{2} on \u{1}Friday\u{2}"),
            vec![
                ("…to ".to_string(), false),
                ("Oslo".to_string(), true),
                (" on ".to_string(), false),
                ("Friday".to_string(), true),
            ]
        );
    }
}
