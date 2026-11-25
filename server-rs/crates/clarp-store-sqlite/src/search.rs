//! The message search index (issue 803): what agents' conversations said, for `GET /search`.
//!
//! A file of its own beside the fleet's (`search.db`), owned by one thread of the Host: nothing
//! an agent writes waits for it. It is derived from the agents' logs and can be thrown away; the
//! Host then builds it again. A row's words and how far its agent has been read are written in
//! one transaction, so after a crash the index stands where its agent's mark says it does.
//! Hence `synchronous=NORMAL`: what a power cut takes back, the next read gives again. A file
//! that will not open, or that SQLite finds damaged, is thrown away and built again.
//!
//! One connection writes ([`SearchIndex`]), on the thread that keeps the index; a search reads
//! on a connection of its own, opened read-only ([`SearchReader`]), so a search never waits for
//! a page being written and never writes.
//!
//! The words are kept in an FTS5 table, folded to lower case and without accents
//! (`unicode61 remove_diacritics 2`: "på" is found by "pa" as by "på"; ø and æ are letters of
//! their own and stay). Every word asked for must be in a row, each as the start of a word, so
//! "møte" finds "møtet" and "Oslo" finds "Oslofjorden".

use std::collections::HashMap;
use std::fmt;
use std::path::Path;

use rusqlite::{Connection, ErrorCode, OpenFlags, OptionalExtension, params};

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
CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS agent (
    agent_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    begins_after INTEGER NOT NULL,
    older_than TEXT,
    complete INTEGER NOT NULL
) STRICT;
";

/// SQLite's own files of the index, beside the index's file: what is thrown away with it.
const SIDE_FILES: [&str; 3] = ["-wal", "-shm", "-journal"];

/// A row's id is its time and a number for rows of the same millisecond: `ts_ms * SAME_MS + n`.
/// So the full-text table, whose rows it numbers, holds them in the order of their time, and a
/// search walks its matches newest first and stops at a page's end: it never sorts every row
/// that has a common word (4.4 s for "the" at 300 000 rows when it did, 25 Nov 2026).
const SAME_MS: i64 = 1 << 10;
/// How far past its millisecond a row is numbered when its millisecond is full: one second.
const SPILL: i64 = SAME_MS * 1_000;

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
    /// The file is damaged or is no database: it was thrown away and the index begins anew.
    Broken(String),
}

impl fmt::Display for SearchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Asked(why) => write!(f, "{why}"),
            Self::Storage(why) => write!(f, "search index failed: {why}"),
            Self::Broken(why) => write!(f, "search index was damaged and begins anew: {why}"),
        }
    }
}

impl std::error::Error for SearchError {}

impl From<rusqlite::Error> for SearchError {
    fn from(error: rusqlite::Error) -> Self {
        let damaged = matches!(
            error.sqlite_error_code(),
            Some(ErrorCode::DatabaseCorrupt | ErrorCode::NotADatabase)
        );
        if damaged {
            Self::Broken(error.to_string())
        } else {
            Self::Storage(error.to_string())
        }
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

/// The index as its one writer has it.
pub struct SearchIndex {
    db: Connection,
    #[cfg(test)]
    path: std::path::PathBuf,
}

impl SearchIndex {
    /// Opens the index at `path`, creating it when it is not there. A file that does not open
    /// as the index is thrown away and made anew: what it held is read again from the logs.
    /// Answers whether it began anew.
    pub fn open(path: &Path) -> Result<(Self, bool), SearchError> {
        match Self::open_once(path) {
            Ok(index) => Ok((index, false)),
            Err(why) => {
                eprintln!("search index: {why}; it is made anew");
                discard(path);
                Ok((Self::open_once(path)?, true))
            }
        }
    }

    fn open_once(path: &Path) -> Result<Self, SearchError> {
        let db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "NORMAL")?;
        db.execute_batch(SCHEMA)?;
        // A file that is not what this index writes is found here, not at the first search.
        db.query_row("SELECT count(*) FROM agent", [], |row| row.get::<_, i64>(0))?;
        private(path);
        Ok(Self {
            db,
            #[cfg(test)]
            path: path.to_path_buf(),
        })
    }

    /// A search as a reader of this index makes it.
    #[cfg(test)]
    fn search(&self, asked: Asked<'_>) -> Result<Found, SearchError> {
        let reader = SearchReader::open(&self.path)?;
        reader
            .ok_or_else(|| SearchError::Storage("no file".into()))?
            .search(asked)
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
                // The first number of its millisecond that no row has yet; where a thousand
                // rows share it (a conversation brought whole, stamped with one time), the
                // next free number within the second after it: such a row is ordered a
                // little later than its time, and still kept.
                let first = row.ts_ms.clamp(0, i64::MAX / SPILL - 1) * SAME_MS;
                let mut id = None;
                for reach in [SAME_MS, SPILL] {
                    let taken: Option<i64> = tx.query_row(
                        "SELECT max(id) FROM hit WHERE id BETWEEN ?1 AND ?2",
                        params![first, first + reach - 1],
                        |found| found.get(0),
                    )?;
                    let free = taken.map_or(first, |taken| taken + 1);
                    if free < first + reach {
                        id = Some(free);
                        break;
                    }
                }
                // A million rows within one second: the rest of them are not kept.
                let Some(id) = id else {
                    continue;
                };
                tx.execute(
                    "INSERT INTO hit(id, agent_id, row_id, role, ts_ms, revision) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        id,
                        agent_id,
                        row.row_id,
                        row.role,
                        row.ts_ms,
                        signed(row.revision)
                    ],
                )?;
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
}

/// A search's own connection to the index, read-only.
pub struct SearchReader {
    db: Connection,
}

impl SearchReader {
    /// Opens the index at `path` to read; `None` while its writer has not made it yet.
    pub fn open(path: &Path) -> Result<Option<Self>, SearchError> {
        if !path.exists() {
            return Ok(None);
        }
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let db = Connection::open_with_flags(path, flags)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.pragma_update(None, "query_only", true)?;
        Ok(Some(Self { db }))
    }

    /// The rows of these agents that have every word asked, newest first.
    pub fn search(&self, asked: Asked<'_>) -> Result<Found, SearchError> {
        let query = fts_query(asked.words)
            .ok_or_else(|| SearchError::Asked("nothing to search for in q".to_string()))?;
        let before = asked.before.map(cursor).transpose()?;
        let limit = asked.limit.clamp(1, MOST);
        let agents = json_list(asked.agents);
        let tx = self.db.unchecked_transaction()?;
        let mut hits = Vec::new();
        {
            let mut statement = tx.prepare(
                "SELECT h.id, h.agent_id, h.row_id, h.role, h.ts_ms, h.revision, \
                 snippet(words, 0, char(1), char(2), '…', ?2) \
                 FROM words CROSS JOIN hit h ON h.id = words.rowid \
                 WHERE words MATCH ?1 AND words.rowid < ?3 \
                 AND h.agent_id IN (SELECT value FROM json_each(?4)) \
                 ORDER BY words.rowid DESC LIMIT ?5",
            )?;
            let rows = statement.query_map(
                params![
                    query,
                    SNIPPET_WORDS,
                    before.unwrap_or(i64::MAX),
                    agents,
                    i64::try_from(limit + 1).unwrap_or(i64::MAX),
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
        // Read in the same snapshot as the hits.
        let behind: i64 = tx.query_row(
            "SELECT count(*) FROM json_each(?1) a LEFT JOIN agent m ON m.agent_id = a.value \
             WHERE m.complete IS NOT 1",
            [&agents],
            |row| row.get(0),
        )?;
        tx.finish()?;
        let more = hits.len() > limit;
        hits.truncate(limit);
        let next = more
            .then(|| hits.last().map(|(id, _)| id.to_string()))
            .flatten();
        Ok(Found {
            hits: hits.into_iter().map(|(_, hit)| hit).collect(),
            next,
            partial: behind > 0,
        })
    }
}

/// Throws the index away with SQLite's files beside it: it is derived, and is built again.
pub fn discard(path: &Path) {
    let _ = std::fs::remove_file(path);
    for side in SIDE_FILES {
        let mut name = path.as_os_str().to_owned();
        name.push(side);
        let _ = std::fs::remove_file(std::path::PathBuf::from(name));
    }
}

/// Texts as a JSON array, for `json_each`.
fn json_list(texts: &[String]) -> String {
    let mut list = String::from("[");
    for (at, text) in texts.iter().enumerate() {
        if at > 0 {
            list.push(',');
        }
        list.push('"');
        for sign in text.chars() {
            match sign {
                '"' => list.push_str("\\\""),
                '\\' => list.push_str("\\\\"),
                sign if u32::from(sign) < 0x20 => {
                    list.push_str(&format!("\\u{:04x}", u32::from(sign)));
                }
                sign => list.push(sign),
            }
        }
        list.push('"');
    }
    list.push(']');
    list
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

/// The cursor of a page: the id of the last hit on the page before it.
fn cursor(text: &str) -> Result<i64, SearchError> {
    text.trim()
        .parse()
        .ok()
        .filter(|id: &i64| *id > 0)
        .ok_or_else(|| SearchError::Asked("unknown search cursor".to_string()))
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
        let (index, anew) = SearchIndex::open(&dir.join("search.db")).unwrap();
        assert!(!anew);
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
        assert!(
            hit.parts
                .iter()
                .any(|(text, on)| !on && text.contains("train"))
        );
        assert_eq!(
            (hit.agent_id.as_str(), hit.ts_ms, hit.revision),
            ("a1", 2_000, 2)
        );
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
            .apply(
                "a1",
                &[row("r1", 1, 5, "first draft about apples")],
                &mark(5),
            )
            .unwrap();
        index
            .apply(
                "a1",
                &[row("r1", 1, 9, "final answer about pears")],
                &mark(9),
            )
            .unwrap();
        assert!(ids(&mut index, "apples", &["a1"]).is_empty());
        assert_eq!(ids(&mut index, "pears", &["a1"]), ["r1"]);
        // A page read again from before (a backfill behind a live update) is older: kept out.
        index
            .apply(
                "a1",
                &[row("r1", 1, 5, "first draft about apples")],
                &mark(9),
            )
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
        index
            .apply("a1", &[row("w1", 1, 2, "  ")], &mark(2))
            .unwrap();
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
            .apply(
                "gone",
                &[row("g1", 1, 1, "secret plans for lunch")],
                &mark(1),
            )
            .unwrap();
        index
            .apply(
                "kept",
                &[row("k1", 2, 1, "public plans for lunch")],
                &mark(1),
            )
            .unwrap();
        index.forget("gone").unwrap();
        assert_eq!(ids(&mut index, "lunch", &["gone", "kept"]), ["k1"]);
        assert!(!index.marks().unwrap().contains_key("gone"));
        let words: i64 = index
            .db
            .query_row(
                "SELECT count(*) FROM words WHERE words MATCH 'secret'",
                [],
                |row| row.get(0),
            )
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
        let (index, _scratch) = index();
        let allowed = agents(&["a1"]);
        let asked = |words, before| Asked {
            words,
            agents: &allowed,
            limit: 5,
            before,
        };
        assert!(matches!(
            index.search(asked("  \"?! ", None)),
            Err(SearchError::Asked(_))
        ));
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
        index
            .apply("a1", &[row("f1", 1, 1, &forged)], &mark(1))
            .unwrap();
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
        index
            .apply("a1", &[row("t1", 1, 1, "tea")], &begun)
            .unwrap();
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
    fn a_damaged_file_is_thrown_away_and_the_index_begins_anew() {
        let (index, scratch) = index();
        drop(index);
        let path = scratch.0.join("search.db");
        for side in SIDE_FILES {
            let mut name = path.as_os_str().to_owned();
            name.push(side);
            let _ = std::fs::remove_file(std::path::PathBuf::from(name));
        }
        std::fs::write(
            &path,
            b"this is not what the index writes, and is long enough",
        )
        .unwrap();
        let (mut index, anew) = SearchIndex::open(&path).unwrap();
        assert!(anew);
        assert!(index.marks().unwrap().is_empty());
        index
            .apply("a1", &[row("d1", 1, 1, "fresh start")], &mark(1))
            .unwrap();
        assert_eq!(ids(&mut index, "fresh", &["a1"]), ["d1"]);
    }

    #[test]
    fn a_reader_reads_only_and_finds_nothing_before_the_index_is_made() {
        let (index, scratch) = index();
        assert!(
            SearchReader::open(&scratch.0.join("absent.db"))
                .unwrap()
                .is_none()
        );
        let reader = SearchReader::open(&scratch.0.join("search.db"))
            .unwrap()
            .unwrap();
        assert!(reader.db.execute("DELETE FROM agent", []).is_err());
        drop(index);
    }

    #[test]
    fn agents_are_listed_for_sqlite_as_json_whatever_their_signs() {
        assert_eq!(json_list(&[]), "[]");
        let odd = vec!["a\"b".to_string(), "c\\d".to_string(), "e\u{1}".to_string()];
        assert_eq!(json_list(&odd), r#"["a\"b","c\\d","e\u0001"]"#);
    }

    /// What the index costs at the size of a real Host's history (632 agents, about 300 000
    /// rows, about 215 MB of words), on generated words only. Run by hand, in release:
    /// `cargo test --release -p clarp-store-sqlite --lib -- --ignored --nocapture measure`.
    #[test]
    #[ignore = "a measurement, run by hand"]
    fn measure_an_index_of_a_large_hosts_size() {
        const AGENTS: usize = 632;
        const ROWS: usize = 300_000;
        const BYTES: usize = 215_000_000;
        const PAGE: usize = 200;
        const PACE_MS: f64 = 250.0;
        let mut seed: u64 = 0x9e37_79b9_7f4a_7c15;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        // A vocabulary of made-up words whose use falls off as in speech: a few very common.
        let syllables = [
            "ka", "lo", "mi", "ne", "su", "ta", "ri", "vo", "be", "ø", "på", "en",
        ];
        let mut vocabulary: Vec<String> = ["the", "and", "to", "of", "a", "er", "og", "det"]
            .iter()
            .map(|word| word.to_string())
            .collect();
        while vocabulary.len() < 20_000 {
            let length = 2 + (next() % 3) as usize;
            let word: String = (0..length)
                .map(|_| syllables[(next() % syllables.len() as u64) as usize])
                .collect();
            vocabulary.push(word);
        }
        let per_row = BYTES / ROWS;
        let (index, scratch) = index();
        let path = scratch.0.join("search.db");
        let mut index = index;
        let mut row_number = 0_usize;
        let mut slowest = std::time::Duration::ZERO;
        let mut pages = 0_usize;
        let started = std::time::Instant::now();
        for agent in 0..AGENTS {
            let agent_id = format!("agent-{agent:04}");
            let rows_here = ROWS / AGENTS + usize::from(agent < ROWS % AGENTS);
            let mut written = 0;
            while written < rows_here {
                let mut page = Vec::with_capacity(PAGE);
                for _ in 0..PAGE.min(rows_here - written) {
                    let mut text = String::with_capacity(per_row + 16);
                    while text.len() < per_row {
                        // Zipf-like: the square of a uniform draw favours the first words.
                        let draw = (next() % 1_000_000) as f64 / 1_000_000.0;
                        let at = (draw * draw * draw * vocabulary.len() as f64) as usize;
                        text.push_str(&vocabulary[at.min(vocabulary.len() - 1)]);
                        text.push(' ');
                    }
                    if row_number % 40_000 == 7 {
                        text.push_str("Zanzibarquest");
                    }
                    page.push(row(
                        &format!("r-{row_number}"),
                        1_700_000_000_000 + row_number as i64 * 1_000,
                        row_number as u64 + 1,
                        &text,
                    ));
                    row_number += 1;
                    written += 1;
                }
                let one = std::time::Instant::now();
                let mark = Mark {
                    revision: row_number as u64,
                    begins_after: 0,
                    older_than: page.first().map(|row| row.row_id.clone()),
                    complete: written >= rows_here,
                };
                index.apply(&agent_id, &page, &mark).unwrap();
                slowest = slowest.max(one.elapsed());
                pages += 1;
            }
        }
        let writing = started.elapsed();
        index
            .db
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")
            .unwrap();
        let size = std::fs::metadata(&path).unwrap().len();
        let all: Vec<String> = (0..AGENTS)
            .map(|agent| format!("agent-{agent:04}"))
            .collect();
        let reader = SearchReader::open(&path).unwrap().unwrap();
        let time = |words: &str, all: &[String]| {
            let mut found = 0;
            let started = std::time::Instant::now();
            for _ in 0..5 {
                found = reader
                    .search(Asked {
                        words,
                        agents: all,
                        limit: 20,
                        before: None,
                    })
                    .unwrap()
                    .hits
                    .len();
            }
            (started.elapsed() / 5, found)
        };
        let per_page = writing / pages as u32;
        let paced_s = pages as f64 * PACE_MS.max(per_page.as_secs_f64() * 1000.0) / 1000.0;
        println!(
            "rows {row_number}, agents {AGENTS}, pages {pages}, words ~{} MB",
            BYTES / 1_000_000
        );
        println!("index {:.1} MB after a checkpoint", size as f64 / 1e6);
        println!(
            "writing without pauses {:.1} s, {:.1} ms a page on average, slowest {:.1} ms",
            writing.as_secs_f64(),
            per_page.as_secs_f64() * 1000.0,
            slowest.as_secs_f64() * 1000.0
        );
        println!("backfill at one page per {PACE_MS} ms: {:.0} s", paced_s);
        for words in ["the", "og", "zanzibarquest", "the og", "ka"] {
            let (took, found) = time(words, &all);
            println!(
                "search {words:?}: {:.1} ms, {found} hits",
                took.as_secs_f64() * 1000.0
            );
        }
        // The worst case: a common word in the one chat whose rows are the oldest of all, so
        // every newer match is walked past.
        let oldest = vec!["agent-0000".to_string()];
        for words in ["the", "zanzibarquest"] {
            let (took, found) = time(words, &oldest);
            println!(
                "search {words:?} in the oldest chat alone: {:.1} ms, {found} hits",
                took.as_secs_f64() * 1000.0
            );
        }
    }

    #[test]
    fn rows_of_one_millisecond_are_all_kept_past_a_thousand() {
        let (mut index, _scratch) = index();
        let rows: Vec<Row> = (0..1_500)
            .map(|n| row(&format!("same-{n}"), 5_000, n + 1, "brought whole"))
            .collect();
        index.apply("a1", &rows, &mark(1_500)).unwrap();
        index
            .apply("a1", &[row("later", 6_000, 2_000, "brought later")], &mark(2_000))
            .unwrap();
        let kept: i64 = index
            .db
            .query_row("SELECT count(*) FROM hit", [], |row| row.get(0))
            .unwrap();
        assert_eq!(kept, 1_501);
        // The newest row is still found first.
        assert_eq!(ids(&mut index, "brought", &["a1"])[0], "later");
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
