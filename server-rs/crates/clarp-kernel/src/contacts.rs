//! Who an agent is when nobody named it: the contacts a tool's agent may take, and the ones
//! that are minted when none is free (feature `auto_contact_minting`, contract 85).
//!
//! A contact is a persona: a name with its voice and way of speaking. An agent made with
//! `auto_contact` takes the first free contact of its tool's pool, and a new one is minted
//! when the pool is empty; one made with `mint_contact` always takes a new one; and
//! `POST /agent-assign` gives a running agent a contact. A minted contact is a persona of the
//! owner's with one of the curated given names of its tool's tier, a voice nobody speaks with
//! (`personas::picked_voice`) and one of the tier's ways of speaking. Nothing outside the
//! Host is asked for any of it.
//!
//! The rules are the Python Host's own (`server/lib/contact_minting.py`,
//! `contact_assignment.py`, `roster.py`, `policies/agent_spec.py`), and the tiers' names and
//! ways of speaking are its lists, carried as data (`contact-tiers.json`). Both are held to
//! what its functions answer (`tests/fixtures/contact-vectors.json`, written by
//! `server-rs/tools/contact-vectors.py`).

use std::collections::HashSet;
use std::sync::LazyLock;

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::{personas, said};

/// A tier: the kind of tool its contacts were made for, with the names a contact of it is
/// minted with, in the order they are given out, and its ways of speaking.
#[derive(Debug, Deserialize)]
struct Tier {
    id: String,
    minted_names: Vec<String>,
    personalities: Vec<String>,
}

fn tiers() -> &'static [Tier] {
    static TIERS: LazyLock<Vec<Tier>> = LazyLock::new(|| {
        serde_json::from_str(include_str!("contact-tiers.json")).unwrap_or_default()
    });
    &TIERS
}

/// A tier by its id, or the first where there is none of that id (`contact_tier_spec`).
fn tier_spec(id: &str) -> Option<&'static Tier> {
    let id = folded(id.trim());
    tiers()
        .iter()
        .find(|tier| tier.id == id)
        .or_else(|| tiers().first())
}

fn folded(text: &str) -> String {
    text.to_lowercase()
}

/// The tier of a tool's contacts (`roster.backend_to_tier`): its own for Codex (and
/// OpenCode, which shares it), Antigravity (`gemini`), Grok and DeepSeek; Claude's for any
/// other.
pub fn tier_of_tool(backend: &str) -> &'static str {
    match folded(backend.trim()).as_str() {
        "gemini" | "agy" => "gemini",
        "codex" | "opencode" => "codex",
        "grok" => "grok",
        "deepseek" => "deepseek",
        _ => "claude",
    }
}

/// The tool a tier's contacts run on (`roster.TIER_BACKENDS`), or none for a tier that names
/// none.
pub fn tool_of_tier(tier: &str) -> Option<&'static str> {
    Some(match tier {
        "claude" => "claude",
        "codex" | "janitor" | "janitors" => "codex",
        "grok" => "grok",
        "gemini" => "agy",
        "deepseek" => "deepseek",
        _ => return None,
    })
}

/// Whether a contact may run on a tool, and the tool its tier names where it names one
/// (`roster.validate_contact_backend`). `persona_tier` is the tier its row gives it; without
/// one, its name's ([`personas::tier`]). A contact of no tier runs on any. It is a
/// recommendation for an agent made by name; the pool holds to it.
pub fn allows(name: &str, backend: &str, persona_tier: &str) -> (bool, Option<&'static str>) {
    let given = folded(persona_tier.trim());
    let tier = if given.is_empty() {
        personas::tier(name)
    } else {
        given.as_str()
    };
    match tool_of_tier(tier) {
        None => (true, None),
        Some(tool) => (folded(backend.trim()) == tool, Some(tool)),
    }
}

/// The free name a new contact of a tool is minted with, and its tier
/// (`contact_minting.pick_given_name`): the first of the tier's names nobody has, then one of
/// them with an initial after it (`Clara B`), then with a number (`Clara 2`). Taken is every
/// name an agent goes by (`occupied`), every name of the roster's tiers, and every persona's
/// (`existing`, the built-in ones among them), without the letters' case. `None`: every name
/// there is, is taken.
pub fn given_name(
    backend: &str,
    occupied: &[String],
    existing: &[String],
) -> Option<(String, &'static str)> {
    const INITIALS: &str = "BCDEFGHJKLMNPQRSTVWXYZ";
    let tier = tier_of_tool(backend);
    let names = &tier_spec(tier)?.minted_names;
    let mut taken: HashSet<String> = occupied
        .iter()
        .chain(existing)
        .map(|name| folded(name.trim()))
        .filter(|name| !name.is_empty())
        .collect();
    taken.extend(personas::tier_names().map(str::to_string));
    let free = |candidate: &str| !taken.contains(&folded(candidate));
    let plain = names.iter().map(String::to_string);
    let initialed = INITIALS
        .chars()
        .flat_map(|initial| names.iter().map(move |name| format!("{name} {initial}")));
    let numbered =
        (2..10_000).flat_map(|number| names.iter().map(move |name| format!("{name} {number}")));
    plain
        .chain(initialed)
        .chain(numbered)
        .find(|candidate| free(candidate))
        .map(|name| (name, tier))
}

/// The way a minted contact speaks (`contact_minting.default_personality`): one of its tier's,
/// by the first four bytes of the SHA-256 of its name without the letters' case.
pub fn personality(name: &str, tier: &str) -> &'static str {
    let Some(spec) = tier_spec(tier) else {
        return "";
    };
    if spec.personalities.is_empty() {
        return "";
    }
    let digest = Sha256::digest(folded(name.trim()).as_bytes());
    let index = u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]]) as usize;
    &spec.personalities[index % spec.personalities.len()]
}

/// A persona as the pool reads it: its name, its voices as they are stored, and the tier its
/// row gives it (empty: its name's).
#[derive(Debug, Clone, Copy)]
pub struct Contact<'a> {
    pub name: &'a str,
    pub voice_id: &'a str,
    pub tier: &'a str,
}

/// An agent as the pool reads it: its id, the persona it goes by and its voices.
#[derive(Debug, Clone, Copy)]
pub struct Wearer<'a> {
    pub agent_id: &'a str,
    pub persona: &'a str,
    pub voice_id: &'a str,
}

/// The contacts an agent of a tool may take (`contact_assignment.available_contacts`), in
/// the order of the personas: those no other agent goes by, whose tier allows the tool, and
/// whose Cartesia voice no other agent speaks with. `exclude`: the agent asking, whose own
/// contact counts as free.
pub fn pool<'a>(
    contacts: &[Contact<'a>],
    agents: &[Wearer<'_>],
    backend: &str,
    exclude: &str,
) -> Vec<&'a str> {
    let others: Vec<&Wearer<'_>> = agents
        .iter()
        .filter(|agent| agent.agent_id != exclude)
        .collect();
    let occupied: HashSet<String> = others
        .iter()
        .map(|agent| folded(agent.persona.trim()))
        .collect();
    let voices: HashSet<String> = others
        .iter()
        .filter_map(|agent| said::resolve_voice(Some(agent.voice_id), "cartesia"))
        .collect();
    contacts
        .iter()
        .filter(|contact| !occupied.contains(&folded(contact.name)))
        .filter(|contact| allows(contact.name, backend, contact.tier).0)
        .filter(|contact| {
            said::resolve_voice(Some(contact.voice_id), "cartesia")
                .is_none_or(|voice| !voices.contains(&voice))
        })
        .map(|contact| contact.name)
        .collect()
}

/// Which contact an agent that is made takes, by what the request says
/// (`policies/agent_spec.py`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Launch {
    /// The request names none of its own: the agent is made by the name it gives.
    AsNamed,
    /// The first free contact of its tool's pool.
    Pooled(String),
    /// A contact minted for it.
    Minted,
    /// Refused, with the status and the word it is refused with.
    Refused(u16, &'static str),
}

/// Python's truth of a value of JSON: nothing, false, zero, an empty text, list or object is
/// false.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(truth)) => *truth,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|number| number != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(fields)) => !fields.is_empty(),
    }
}

/// Which contact an agent that is made takes (`mint_contact` or `auto_contact`, each only
/// when it is `true` itself). Neither goes with `anonymous` or `replace_sid`, nor with the
/// other. `mint_contact` always mints; `auto_contact` takes the pool's first (`pooled`) and
/// mints where the pool is empty. A janitor takes none of them.
pub fn launch(
    asked: &serde_json::Map<String, Value>,
    pooled: Option<&str>,
    janitor: bool,
) -> Launch {
    let mint = asked.get("mint_contact") == Some(&Value::Bool(true));
    let auto = asked.get("auto_contact") == Some(&Value::Bool(true));
    if !(mint || auto) || janitor {
        return Launch::AsNamed;
    }
    if truthy(asked.get("anonymous")) || truthy(asked.get("replace_sid")) || (mint && auto) {
        return Launch::Refused(400, "conflicting launch options");
    }
    match pooled {
        Some(name) if !mint => Launch::Pooled(name.to_string()),
        _ => Launch::Minted,
    }
}

/// What `POST /agent-assign` does for an agent (`contact_assignment.assign_contact`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Assigned {
    /// `options`: the names of the contacts it may take.
    Options(Vec<String>),
    /// It takes this contact, which is there.
    Take(String),
    /// It takes a contact minted for it.
    Mint,
    /// It takes a new persona of this name, which is made for it.
    Create(String),
}

/// Why `POST /agent-assign` refused: the status, the word, and the words for a person where
/// they are other than the word.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unassigned {
    pub status: u16,
    pub error: &'static str,
    pub message: &'static str,
}

const fn unassigned(status: u16, error: &'static str) -> Unassigned {
    Unassigned {
        status,
        error,
        message: error,
    }
}

/// The agent a contact is assigned to, as the rule reads it.
#[derive(Debug, Clone, Copy)]
pub struct Assignee<'a> {
    pub agent_id: &'a str,
    pub persona: &'a str,
    pub backend: &'a str,
    pub janitor: bool,
}

/// What a name sent with an assignment says: its words with every run of white space one
/// space (`" ".join(str(name or "").split())`).
pub fn assigned_name(said: Option<&Value>) -> String {
    let text = match said {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(truth)) => if *truth { "True" } else { "False" }.to_string(),
        Some(Value::Number(number)) => number.to_string(),
        Some(other) => other.to_string(),
    };
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// The modes of `POST /agent-assign`.
pub const MODES: [&str; 4] = ["auto", "choose", "create", "options"];

/// What `POST /agent-assign` does (`contact_assignment.assign_contact`), in its order:
/// a mode it does not know, an agent that is not there, a janitor; then by the mode. `auto`
/// keeps the agent's own contact where it is in its pool, else takes the pool's first, else
/// mints one; `choose` takes any persona of that name (without the letters' case) that no
/// other agent goes by, of any tier; `create` makes a persona of a name nobody has.
/// `contacts`: every persona, in their order; `agents`: every agent, the assignee among them.
pub fn assign(
    mode: &str,
    name: Option<&Value>,
    agent: Option<Assignee<'_>>,
    contacts: &[Contact<'_>],
    agents: &[Wearer<'_>],
) -> Result<Assigned, Unassigned> {
    if !MODES.contains(&mode) {
        return Err(unassigned(400, "invalid assignment mode"));
    }
    let Some(agent) = agent else {
        return Err(unassigned(404, "no such agent"));
    };
    if agent.janitor {
        return Err(unassigned(409, "janitor_managed"));
    }
    let occupied: HashSet<String> = agents
        .iter()
        .filter(|other| other.agent_id != agent.agent_id)
        .map(|other| folded(other.persona.trim()))
        .collect();
    let choices = pool(contacts, agents, agent.backend, agent.agent_id);
    let name = assigned_name(name);
    match mode {
        "options" => Ok(Assigned::Options(
            choices.iter().map(|name| (*name).to_string()).collect(),
        )),
        "auto" => Ok(choices
            .iter()
            .find(|choice| **choice == agent.persona)
            .or(choices.first())
            .map_or(Assigned::Mint, |choice| {
                Assigned::Take((*choice).to_string())
            })),
        "choose" => {
            let wanted = folded(&name);
            let Some(contact) = contacts
                .iter()
                .find(|contact| folded(contact.name) == wanted)
            else {
                return Err(unassigned(404, "contact not found"));
            };
            if occupied.contains(&folded(contact.name)) {
                return Err(Unassigned {
                    status: 409,
                    error: "contact_unavailable",
                    message: "This contact already has an active session.",
                });
            }
            Ok(Assigned::Take(contact.name.to_string()))
        }
        _ => {
            if name.is_empty() || name.chars().count() > 60 {
                return Err(unassigned(400, "name required (maximum 60 characters)"));
            }
            let exists = contacts
                .iter()
                .any(|contact| personas::same(contact.name, &name));
            if occupied.contains(&folded(&name)) || exists {
                return Err(unassigned(409, "contact already exists"));
            }
            Ok(Assigned::Create(name))
        }
    }
}
