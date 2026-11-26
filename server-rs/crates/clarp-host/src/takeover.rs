//! Provider evidence read by the agent worker while the shim holds its tool.

use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use clarp_actor::takeover::{Offer, TransitOffer};
use clarp_backend::{agy, history, past};
use clarp_kernel::takeover::{self, ClaudeCheckpointProgress, Input};
use serde_json::Value;

// If the provider history cannot be judged within a modest read, the follow-up waits.
const TRANSCRIPT_MOST: u64 = 32 * 1024 * 1024;

fn transcript(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut bytes = Vec::new();
    file.by_ref()
        .take(TRANSCRIPT_MOST + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > TRANSCRIPT_MOST {
        return None;
    }
    String::from_utf8(bytes).ok()
}

pub(super) fn plan(
    backend: &str,
    home: &Path,
    cwd: &str,
    offer: &Offer,
) -> Option<(String, Input)> {
    let known = offer.session.as_deref();
    match backend {
        "claude" => {
            let projects = crate::past::projects(home);
            let source = known.map(|session| past::claude_source(&projects, cwd, session, true));
            let text = match source {
                Some(Ok(Some(path))) => Some(transcript(&path)?),
                Some(Err(_)) => return None,
                Some(Ok(None)) | None => None,
            };
            let drops_turn = offer.first_prompt_uuid.clone()?;
            let facts = takeover::claude_facts(text.as_deref(), &drops_turn);
            let input =
                takeover::claude_input(facts, offer.first_turn, offer.first.clone(), drops_turn)?;
            let session = if matches!(input, Input::ClaudeFresh { .. }) {
                crate::ids::uuid()
            } else {
                known?.to_string()
            };
            Some((session, input))
        }
        "agy" | "agy-acp" => {
            let session = known?;
            let path = past::agy_transcript(&home.join(agy::OWN), session)?;
            let text = transcript(&path)?;
            if text.lines().any(|line| {
                !line.trim().is_empty() && serde_json::from_str::<serde_json::Value>(line).is_err()
            }) {
                return None;
            }
            let turns = history::agy(&text);
            takeover::agy_can_take_over(Some(&turns), &offer.first).then(|| {
                (
                    session.to_string(),
                    Input::Agy {
                        first: offer.first.clone(),
                    },
                )
            })
        }
        _ => None,
    }
}

/// Read the native history after the exact prompt. Unmatched tool results, extra inputs,
/// and unrecognized activity refuse the checkpoint.
pub(super) fn native_transit_progress(
    home: &Path,
    cwd: &str,
    offer: &TransitOffer,
) -> Option<ClaudeCheckpointProgress> {
    let paths = crate::past::projects(home);
    let path = match offer.session.as_deref() {
        Some(session) => past::claude_source(&paths, cwd, session, true).ok()?,
        None => None,
    };
    let Some(path) = path else {
        return offer.first_turn.then(ClaudeCheckpointProgress::default);
    };
    let text = transcript(&path)?;
    let mut found = false;
    let mut progress = ClaudeCheckpointProgress::default();
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let event = serde_json::from_str::<Value>(line).ok()?;
        if event["uuid"].as_str() == Some(offer.prompt_uuid.as_str()) {
            if found || event["type"] != "user" {
                return None;
            }
            found = true;
        } else if found {
            progress.record(&event)?;
        }
    }
    found.then_some(())?;
    progress.finished()
}

pub(super) async fn can_use(
    backend: &str,
    program: PathBuf,
    versions: &crate::cli_version::Cache,
) -> bool {
    if !matches!(backend, "claude" | "agy" | "agy-acp") {
        return false;
    }
    versions
        .answer(program)
        .await
        .is_some_and(|output| takeover::supported_version(backend, &output))
}
