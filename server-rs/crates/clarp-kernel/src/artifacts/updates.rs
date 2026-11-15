//! Updates: what agents made that their user may want to look at, a page at a time.
//!
//! The apps show it as one list: what failed first, then what is ready to be looked at, then
//! what is being worked on, each the oldest first. A page ends with a cursor that goes on
//! from where it ended, as long as nothing that is shown has changed since: the cursor names
//! the list it was cut from, and a list that is another is asked for from its beginning.
//!
//! The rules are the Python Host's (`server/lib/attention_index.py`). What a cursor and the
//! name of a list look like is each Host's own: a client hands a cursor back and compares
//! names, and reads neither.

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Artifact, Refused, Shelf, refuse};
use crate::python::canonical;

/// The kinds of artifact that are shown. A run of a workflow is not among them: its result
/// is nothing a user acts on from here. Requests and plans have lists of their own.
pub const TYPES: [&str; 11] = [
    "countdown",
    "document",
    "research",
    "file",
    "audio",
    "video",
    "code_change",
    "data",
    "release",
    "directory",
    "html_form",
];

/// What a cursor of a list that is no longer the list is refused with.
pub const STALE: &str = "attention snapshot changed; restart pagination";

/// What is no cursor is refused with.
const NO_CURSOR: &str = "invalid attention cursor";

/// The version of the list's own rules: what a client compares before it trusts an order.
pub const INDEX_VERSION: u64 = 1;

/// The most a page holds.
const MOST: i64 = 200;

/// Where an artifact stands for its user: `blocking` when it failed, `review` when it is
/// ready or done, `working` while it is being made. `None` when it is not shown: a kind
/// that has a list of its own, a draft, what was called off or ran out, what was thrown
/// away.
pub fn bucket(artifact: &Artifact) -> Option<&'static str> {
    if !TYPES.contains(&artifact.kind.as_str()) || artifact.deleted_at.is_some() {
        return None;
    }
    let kind = artifact.payload.get("attention_kind");
    if kind.and_then(Value::as_str) == Some("janitor_failure") {
        return None;
    }
    // A countdown stands where its clock stands, which this Host does not keep yet.
    if artifact.kind == "countdown" {
        return None;
    }
    match artifact.status.as_str() {
        "failed" => Some("blocking"),
        "active" => Some("working"),
        "ready" | "completed" => Some("review"),
        _ => None,
    }
}

/// One artifact of the list.
struct Shown<'a> {
    shelf: &'a Shelf<'a>,
    artifact: &'a Artifact,
    bucket: &'static str,
}

/// The list: what failed, what is ready, what is in the making, each the oldest first.
fn shown<'a>(shelves: &'a [Shelf<'a>]) -> Vec<Shown<'a>> {
    let mut shown: Vec<Shown<'a>> = shelves
        .iter()
        .flat_map(|shelf| {
            shelf.artifacts.artifacts().filter_map(move |artifact| {
                Some(Shown {
                    shelf,
                    artifact,
                    bucket: bucket(artifact)?,
                })
            })
        })
        .collect();
    let rank = |bucket: &str| match bucket {
        "blocking" => 0,
        "review" => 1,
        _ => 2,
    };
    shown.sort_by(|one, two| {
        let place = |shown: &Shown<'a>| (rank(shown.bucket), shown.artifact.created_at);
        place(one)
            .cmp(&place(two))
            .then_with(|| one.artifact.artifact_id.cmp(&two.artifact.artifact_id))
    });
    shown
}

/// The name of a list: another whenever anything that is shown of it is another. Every
/// change of what an artifact holds moves its `updated_at`, so that stands for the rest.
fn name_of(shown: &[Shown<'_>]) -> String {
    let rows: Vec<Value> = shown
        .iter()
        .map(|shown| {
            let artifact = shown.artifact;
            json!([
                artifact.artifact_id,
                artifact.agent_id,
                artifact.session,
                artifact.kind,
                artifact.title,
                artifact.summary,
                artifact.status,
                artifact.reference_id,
                artifact.created_at,
                artifact.updated_at,
                artifact.completed_at,
                artifact.archived_at,
                artifact.pinned_at,
                shown.shelf.maker.persona,
                shown.bucket,
            ])
        })
        .collect();
    Sha256::digest(canonical(&Value::Array(rows)).as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The signs a cursor is written in (base 64, the alphabet that is safe in an address).
const SIGNS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn written(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let at = |place: usize| u32::from(chunk.get(place).copied().unwrap_or(0));
        let held = (at(0) << 16) | (at(1) << 8) | at(2);
        for (place, shift) in [18u32, 12, 6, 0].into_iter().enumerate() {
            if place <= chunk.len() {
                out.push(char::from(SIGNS[((held >> shift) & 63) as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn read(text: &str) -> Option<Vec<u8>> {
    if !text.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let (mut held, mut bits) = (0u32, 0u32);
    for byte in text.trim_end_matches('=').bytes() {
        let six = SIGNS.iter().position(|sign| *sign == byte)?;
        held = (held << 6) | u32::try_from(six).ok()?;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(u8::try_from((held >> bits) & 0xff).ok()?);
            held &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// Where a cursor says a page begins, when it is a cursor of the list with this name.
fn begins(cursor: &str, name: &str) -> Result<usize, Refused> {
    let said = read(cursor).and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    let Some(Value::Object(said)) = said else {
        return refuse(NO_CURSOR);
    };
    let offset = said
        .get("offset")
        .filter(|_| said.len() == 2 && said.contains_key("revision"))
        .and_then(Value::as_u64);
    let Some(offset) = offset else {
        return refuse(NO_CURSOR);
    };
    if said.get("revision").and_then(Value::as_str) != Some(name) {
        return refuse(STALE);
    }
    Ok(usize::try_from(offset).unwrap_or(usize::MAX))
}

/// A page of the list.
#[derive(Debug, Clone, PartialEq)]
pub struct Page {
    /// The artifacts as they are shown, each with where it stands (`attention_bucket`).
    pub artifacts: Vec<Value>,
    /// Where the next page begins, when there is more.
    pub next_cursor: Option<String>,
    /// The name of the list this page was cut from.
    pub snapshot_revision: String,
    /// How many the whole list has.
    pub total: usize,
}

/// A page of what the agents on these shelves made for their user: `limit` at most (one at
/// least, two hundred at most), from the beginning or from where `cursor` says.
pub fn page(shelves: &[Shelf<'_>], limit: i64, cursor: &str, now: i64) -> Result<Page, Refused> {
    let limit = usize::try_from(limit.clamp(1, MOST)).unwrap_or(1);
    if cursor.chars().count() > 2048 {
        return refuse(NO_CURSOR);
    }
    let shown = shown(shelves);
    let name = name_of(&shown);
    let offset = match cursor {
        "" => 0,
        cursor => begins(cursor, &name)?,
    };
    let artifacts: Vec<Value> = shown
        .iter()
        .skip(offset)
        .take(limit)
        .map(|shown| {
            let (maker, goals) = (&shown.shelf.maker, shown.shelf.goals);
            let mut row = shown.artifact.public(maker.persona, goals, now);
            row["attention_bucket"] = json!(shown.bucket);
            row
        })
        .collect();
    let end = offset.saturating_add(artifacts.len());
    let next_cursor = (end < shown.len())
        .then(|| written(format!("{{\"revision\": \"{name}\", \"offset\": {end}}}").as_bytes()));
    Ok(Page {
        artifacts,
        next_cursor,
        snapshot_revision: name,
        total: shown.len(),
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_cursor_is_read_as_it_was_written() {
        for text in [
            "",
            "a",
            "ab",
            "abc",
            "abcd",
            "{\"revision\": \"x\", \"offset\": 12}",
        ] {
            let cursor = written(text.as_bytes());
            assert_eq!(cursor.len() % 4, 0, "{cursor}");
            assert_eq!(read(&cursor).unwrap(), text.as_bytes(), "{cursor}");
        }
        // As Python writes one.
        assert_eq!(written(b"not json"), "bm90IGpzb24=");
        assert_eq!(written(&[0xfb, 0xff]), "-_8=");
        assert!(read("not a cursor").is_none());
        assert!(read("abc").is_none());
        let name = "x";
        let at = |said: &str| begins(&written(said.as_bytes()), name).map_err(|no| no.message);
        assert_eq!(at("{\"revision\": \"x\", \"offset\": 3}"), Ok(3));
        assert_eq!(
            at("{\"revision\": \"y\", \"offset\": 3}"),
            Err(STALE.to_string())
        );
        assert_eq!(
            at("{\"revision\": 7, \"offset\": 3}"),
            Err(STALE.to_string())
        );
        for no in [
            "not json",
            "[1, 2]",
            "{\"offset\": 0}",
            "{\"revision\": \"x\", \"offset\": -1}",
            "{\"revision\": \"x\", \"offset\": \"1\"}",
            "{\"revision\": \"x\", \"offset\": 1.0}",
            "{\"revision\": \"x\", \"offset\": true}",
            "{\"revision\": \"x\", \"offset\": 0, \"more\": 1}",
        ] {
            assert_eq!(at(no), Err(NO_CURSOR.to_string()), "{no}");
        }
    }
}
