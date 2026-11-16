//! The portraits an agent keeps.
//!
//! An agent wears one picture and may keep two more beside it: pictures it published, which
//! its user can make the one it wears. The rules are the Python Host's
//! (`server/lib/agent_portraits.py`), without its store and without a disk: what is kept is
//! a list, the picture the agent wears is named by its digest, and whether a picture's
//! bytes are still there is the Host's to say.
//!
//! The picture an agent wears is always one of its portraits. A change finds it among them
//! or records it then, under an id that says whose it is and what is in it, so that a list
//! that was read before the change named it by the same id.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::personas::version;
use crate::portraits::quoted;

/// How many portraits an agent keeps at most.
pub const MOST: usize = 3;
/// How the id of a portrait begins that nobody chose from what the agent published: the
/// picture the agent was made with, or was given.
pub const WORN_BEGINS: &str = "portrait_legacy_";
/// Who made a portrait that nobody chose from what the agent published.
pub const WORN_BY: &str = "legacy_avatar";

/// One portrait an agent keeps.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Portrait {
    pub portrait_id: String,
    /// The file the agent published that it was chosen from. None: the picture the agent
    /// was made with.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub media_asset_id: Option<String>,
    /// What its bytes are.
    pub sha256: String,
    pub mime_type: String,
    pub created_by: String,
    pub created_at: i64,
}

/// Why a change to an agent's portraits is not made: the status and the Python Host's words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub status: u16,
    pub why: &'static str,
}

impl Refused {
    const fn new(status: u16, why: &'static str) -> Self {
        Self { status, why }
    }
}

pub const UNKNOWN_SESSION: Refused = Refused::new(404, "unknown session");
pub const NO_SUCH_ASSET: Refused = Refused::new(404, "media asset not found");
pub const ANOTHERS_ASSET: Refused = Refused::new(409, "media asset belongs to another agent");
pub const ASSET_GONE: Refused = Refused::new(409, "media asset content unavailable");
pub const FULL: Refused = Refused::new(409, "an agent can retain at most three portraits");
pub const NO_SUCH_PORTRAIT: Refused = Refused::new(404, "portrait not found");
pub const PORTRAIT_GONE: Refused = Refused::new(409, "portrait content unavailable");
pub const UNSUPPORTED: Refused = Refused::new(400, "unsupported portrait action");
const NOTHING_TO_LET_GO: Refused = Refused::new(409, "portrait primary reconciliation conflict");

/// The id of the portrait that is the picture an agent wears, before and after a change
/// records it.
pub fn worn_id(agent_id: &str, digest: &str) -> String {
    format!(
        "{WORN_BEGINS}{agent_id}_{}",
        digest.get(..24).unwrap_or(digest)
    )
}

/// What kind of picture these bytes begin as, when they are one a portrait can hold.
pub fn picture_kind(begins: &[u8]) -> Option<&'static str> {
    if begins.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if begins.starts_with(b"\xff\xd8\xff") {
        Some("image/jpeg")
    } else if begins.starts_with(b"GIF87a") || begins.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if begins.len() >= 12 && &begins[..4] == b"RIFF" && &begins[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

/// The oldest of the portraits the agent does not wear: by when it was made, then by its id.
fn oldest_beside(kept: &[Portrait], worn: Option<&str>) -> Option<usize> {
    let beside = kept
        .iter()
        .enumerate()
        .filter(|(_, portrait)| Some(portrait.sha256.as_str()) != worn);
    beside
        .min_by_key(|(_, portrait)| (portrait.created_at, portrait.portrait_id.clone()))
        .map(|(at, _)| at)
}

/// The portraits as a change finds them: without the ones beside the worn one whose bytes
/// are gone, and with the picture the agent wears among them. It is recorded now when it
/// was not, and with it as a fourth the oldest of the others is let go.
///
/// `worn` is the digest and the kind of the picture the agent wears, when it wears one a
/// portrait can hold. `there` says whether a portrait's bytes are still kept.
pub fn before_a_change(
    kept: &mut Vec<Portrait>,
    (agent_id, made_at): (&str, i64),
    worn: Option<(&str, &str)>,
    there: impl Fn(&Portrait) -> bool,
) -> Result<(), Refused> {
    let digest = worn.map(|(digest, _)| digest);
    kept.retain(|portrait| Some(portrait.sha256.as_str()) == digest || there(portrait));
    let Some((digest, kind)) = worn else {
        return Ok(());
    };
    if kept.iter().any(|portrait| portrait.sha256 == digest) {
        return Ok(());
    }
    if kept.len() >= MOST {
        let Some(oldest) = oldest_beside(kept, Some(digest)) else {
            return Err(NOTHING_TO_LET_GO);
        };
        kept.remove(oldest);
    }
    kept.push(Portrait {
        portrait_id: worn_id(agent_id, digest),
        media_asset_id: None,
        sha256: digest.to_string(),
        mime_type: kind.to_string(),
        created_by: WORN_BY.to_string(),
        created_at: made_at,
    });
    Ok(())
}

/// A file the agent published is kept as a portrait. One whose bytes a portrait already
/// has is that portrait: nothing is added. `id` is the id a new portrait gets.
pub fn keep(kept: &mut Vec<Portrait>, published: Portrait) -> Result<(), Refused> {
    if kept
        .iter()
        .any(|portrait| portrait.sha256 == published.sha256)
    {
        return Ok(());
    }
    if kept.len() >= MOST {
        return Err(FULL);
    }
    kept.push(published);
    Ok(())
}

/// The portrait of this id among the ones an agent keeps.
pub fn chosen<'a>(kept: &'a [Portrait], portrait_id: &str) -> Result<&'a Portrait, Refused> {
    let found = kept
        .iter()
        .find(|portrait| portrait.portrait_id == portrait_id);
    found.ok_or(NO_SUCH_PORTRAIT)
}

/// The portraits as a reading shows them, which changes nothing: as the next change will
/// find them. The picture the agent wears is among them also when nothing has recorded it
/// yet; the ones beside it whose bytes are gone are left out; and with the worn one as a
/// fourth, the oldest of the others is left out too.
pub fn as_read(
    kept: &[Portrait],
    (agent_id, made_at): (&str, i64),
    worn: Option<(&str, &str)>,
    there: impl Fn(&Portrait) -> bool,
) -> Vec<Portrait> {
    let mut shown = kept.to_vec();
    // What a change would refuse is shown as it stands.
    let _ = before_a_change(&mut shown, (agent_id, made_at), worn, there);
    shown
}

/// What the apps are told of an agent's portraits: the one it wears first, then the others
/// in the order they were made.
pub fn told(
    (session, agent_id): (&str, &str),
    kept: &[Portrait],
    worn: Option<&str>,
    there: impl Fn(&Portrait) -> bool,
) -> Value {
    let mut ordered: Vec<&Portrait> = kept.iter().collect();
    let is_worn = |portrait: &Portrait| Some(portrait.sha256.as_str()) == worn;
    ordered.sort_by_key(|portrait| {
        (
            !is_worn(portrait),
            portrait.created_at,
            portrait.portrait_id.clone(),
        )
    });
    let primary = ordered.iter().find(|portrait| is_worn(portrait));
    let portraits: Vec<Value> = ordered
        .iter()
        .map(|portrait| {
            let available = there(portrait);
            let shown_as = version(&portrait.sha256);
            let url = if available {
                format!(
                    "/agent-portraits/{}/content?v={shown_as}",
                    quoted(&portrait.portrait_id)
                )
            } else {
                String::new()
            };
            let chosen_from_published = portrait.media_asset_id.is_some();
            let created_by = match portrait.created_by.as_str() {
                "" => "agent",
                by => by,
            };
            json!({
                "portrait_id": portrait.portrait_id,
                "media_asset_id": portrait.media_asset_id,
                "role": if is_worn(portrait) { "primary" } else { "alternate" },
                "content_version": shown_as,
                "url": url,
                "source": if chosen_from_published { "media_asset" } else { WORN_BY },
                "created_by": created_by,
                "created_at": portrait.created_at,
                "available": available,
            })
        })
        .collect();
    json!({
        "contract": "agent-portraits.v1",
        "session": session,
        "agent_id": agent_id,
        "primary_portrait_id": primary.map(|portrait| portrait.portrait_id.clone()),
        "portraits": portraits,
        "max_portraits": MOST,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn published(id: &str, digest: &str, at: i64) -> Portrait {
        Portrait {
            portrait_id: format!("portrait_{id}"),
            media_asset_id: Some(format!("asset_{id}")),
            sha256: digest.repeat(64),
            mime_type: "image/png".to_string(),
            created_by: "agent".to_string(),
            created_at: at,
        }
    }

    fn ids(kept: &[Portrait]) -> Vec<&str> {
        kept.iter()
            .map(|portrait| portrait.portrait_id.as_str())
            .collect()
    }

    #[test]
    fn the_picture_an_agent_wears_is_one_of_its_portraits_and_at_most_three_are_kept() {
        let worn = "a".repeat(64);
        let wears = Some((worn.as_str(), "image/jpeg"));
        let all_there = |_: &Portrait| true;
        let mut kept = Vec::new();
        // A change records the picture the agent wears, under the id a reading showed.
        let read = as_read(&kept, ("agent1", 100), wears, all_there);
        before_a_change(&mut kept, ("agent1", 100), wears, all_there).unwrap();
        assert_eq!(kept, read);
        let worn_as = worn_id("agent1", &worn);
        assert_eq!(
            worn_as,
            format!("portrait_legacy_agent1_{}", "a".repeat(24))
        );
        assert_eq!(ids(&kept), [worn_as.as_str()]);
        assert_eq!(
            (kept[0].created_by.as_str(), kept[0].created_at),
            (WORN_BY, 100)
        );

        // Two that it published are kept beside it; a third is one too many, and one whose
        // bytes a portrait already has is that portrait.
        keep(&mut kept, published("b", "b", 300)).unwrap();
        keep(&mut kept, published("c", "c", 200)).unwrap();
        assert_eq!(keep(&mut kept, published("d", "d", 400)), Err(FULL));
        keep(&mut kept, published("again", "b", 500)).unwrap();
        assert_eq!(kept.len(), 3);

        // Told: the one it wears first, then the others as they were made.
        let said = told(("claire", "agent1"), &kept, Some(&worn), all_there);
        let order: Vec<&str> = said["portraits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|portrait| portrait["portrait_id"].as_str().unwrap())
            .collect();
        assert_eq!(order, [worn_as.as_str(), "portrait_c", "portrait_b"]);
        assert_eq!(said["primary_portrait_id"], worn_as.as_str());
        assert_eq!(said["max_portraits"], 3);
        assert_eq!(
            said["portraits"][1],
            json!({
                "portrait_id": "portrait_c", "media_asset_id": "asset_c", "role": "alternate",
                "content_version": "c".repeat(16),
                "url": format!("/agent-portraits/portrait_c/content?v={}", "c".repeat(16)),
                "source": "media_asset", "created_by": "agent", "created_at": 200, "available": true,
            })
        );
        assert_eq!(
            (
                &said["portraits"][0]["role"],
                &said["portraits"][0]["source"]
            ),
            (&json!("primary"), &json!("legacy_avatar"))
        );

        // One of the others is made the one it wears: nothing is added, and it is first.
        assert_eq!(chosen(&kept, "portrait_b").unwrap().sha256, "b".repeat(64));
        assert_eq!(chosen(&kept, "portrait_x"), Err(NO_SUCH_PORTRAIT));
        let now_worn = "b".repeat(64);
        let wears_b = Some((now_worn.as_str(), "image/png"));
        before_a_change(&mut kept, ("agent1", 100), wears_b, all_there).unwrap();
        assert_eq!(kept.len(), 3);
        let said = told(("claire", "agent1"), &kept, Some(&now_worn), all_there);
        assert_eq!(said["primary_portrait_id"], "portrait_b");
        assert_eq!(said["portraits"][1]["portrait_id"], worn_as.as_str());

        // The agent is given a picture nobody recorded: with it as a fourth, the oldest of
        // the others is let go, and a reading shows the same before any change is made.
        let given = "e".repeat(64);
        let wears_e = Some((given.as_str(), "image/webp"));
        let read = as_read(&kept, ("agent1", 100), wears_e, all_there);
        assert_eq!(kept.len(), 3, "a reading changes nothing");
        before_a_change(&mut kept, ("agent1", 100), wears_e, all_there).unwrap();
        assert_eq!(kept, read);
        let given_as = worn_id("agent1", &given);
        assert_eq!(ids(&kept), ["portrait_b", "portrait_c", given_as.as_str()]);

        // A portrait beside the worn one whose bytes are gone is let go at the next change,
        // and a reading leaves it out; the worn one stays whatever became of its bytes.
        let c_gone = |portrait: &Portrait| portrait.portrait_id != "portrait_c";
        assert_eq!(as_read(&kept, ("agent1", 100), wears_e, c_gone).len(), 2);
        let none_there = |_: &Portrait| false;
        before_a_change(&mut kept, ("agent1", 100), wears_e, none_there).unwrap();
        assert_eq!(ids(&kept), [given_as.as_str()]);
        let said = told(("claire", "agent1"), &kept, Some(&given), none_there);
        assert_eq!(
            (
                &said["portraits"][0]["available"],
                &said["portraits"][0]["url"]
            ),
            (&json!(false), &json!(""))
        );
        // An agent that wears no picture a portrait can hold has none recorded for it.
        let mut bare = Vec::new();
        before_a_change(&mut bare, ("agent2", 5), None, all_there).unwrap();
        assert!(bare.is_empty());
        let said = told(("bare", "agent2"), &bare, None, all_there);
        assert_eq!(
            (
                &said["primary_portrait_id"],
                said["portraits"].as_array().unwrap().len()
            ),
            (&Value::Null, 0)
        );
    }

    #[test]
    fn a_picture_is_known_by_how_its_bytes_begin() {
        for (begins, kind) in [
            (&b"\x89PNG\r\n\x1a\n...."[..], Some("image/png")),
            (b"\xff\xd8\xff\xe0....", Some("image/jpeg")),
            (b"GIF87a......", Some("image/gif")),
            (b"GIF89a......", Some("image/gif")),
            (b"RIFF....WEBPVP8 ", Some("image/webp")),
            (b"RIFF....WAVEfmt ", None),
            (b"RIFF", None),
            (b"%PDF-1.7", None),
            (b"", None),
        ] {
            assert_eq!(picture_kind(begins), kind, "{begins:?}");
        }
    }
}
