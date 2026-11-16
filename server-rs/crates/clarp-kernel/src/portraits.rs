//! The face an agent is shown with.
//!
//! A Host ships a picture for many names an agent can have, one for each kind of tool for
//! the names it has none for, and for a few names a picture for each family of model. Which
//! file an agent's row points at follows from the agent's name, its tool and its model; the
//! address carries the file's content, so that an app keeps a picture for as long as it is
//! the same picture.
//!
//! The rules are the Python Host's (`server/lib/avatar_urls.py`, `model_avatars.py`), held to
//! it by `tests/fixtures/portrait-vectors.json`, which `server-rs/tools/portrait-vectors.py`
//! writes. Whether a file is there and what is in it is the Host's to find out; nothing here
//! reads a disk.

/// Where the pictures of names are fetched from.
pub const AVATARS: &str = "/static/avatars";
/// Where the pictures of a name on a family of model are fetched from.
pub const MODEL_AVATARS: &str = "/static/avatars/models";
/// Where an agent's own picture is fetched from.
pub const OWN: &str = "/avatars";

/// The families of model a picture may be drawn for, each with the word that names it in a
/// model's id. Read in this order: `claude-opus-4-6-thinking` is Opus by its own word, not
/// anything by its provider's.
const FAMILIES: &[&str] = &[
    "fable", "opus", "sonnet", "haiku", "gemini", "grok", "codex", "deepseek",
];

/// The name a picture's file has for an agent's name: its lower-case letters, digits,
/// hyphens and underscores.
pub fn slug(name: &str) -> String {
    name.to_lowercase()
        .chars()
        .filter(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '_' | '-'))
        .collect()
}

/// The family of model an id of a model names, or none.
pub fn family_of(model: &str) -> &'static str {
    let model = clarp_speech::listen::strip(model).to_lowercase();
    if model.is_empty() {
        return "";
    }
    FAMILIES
        .iter()
        .find(|family| model.contains(**family))
        .copied()
        .unwrap_or("")
}

/// The family of model a tool names when an agent names no model: the one family the tool
/// runs, for a tool that runs one. Claude Code runs several and names none.
pub fn family_of_tool(backend: &str) -> &'static str {
    match backend {
        "codex" => "codex",
        "agy" => "gemini",
        "grok" => "grok",
        "deepseek" => "deepseek",
        "muse" => "muse",
        _ => "",
    }
}

/// The families to look for a picture of, the best evidence first: the agent's own model,
/// the model its tool is set to on this Host, the family its tool names.
pub fn families(backend: &str, model: &str, default_model: &str) -> Vec<&'static str> {
    let mut ordered = Vec::new();
    for family in [
        family_of(model),
        family_of(default_model),
        family_of_tool(backend),
    ] {
        if !family.is_empty() && !ordered.contains(&family) {
            ordered.push(family);
        }
    }
    ordered
}

/// The files, under [`MODEL_AVATARS`], that may hold the picture of an agent on its model:
/// the first that is there is the one.
pub fn model_files(persona: &str, backend: &str, model: &str, default_model: &str) -> Vec<String> {
    let name = slug(persona);
    if name.is_empty() {
        return Vec::new();
    }
    families(backend, model, default_model)
        .into_iter()
        .map(|family| format!("{name}.{family}.png"))
        .collect()
}

/// The files, under [`AVATARS`], that may hold the picture of an agent that chose none: the
/// one of its name, then the one of its tool. The first that is there is the one.
pub fn files(persona: &str, backend: &str) -> Vec<String> {
    let mut files = Vec::new();
    let name = slug(persona);
    if !name.is_empty() {
        files.push(format!("{name}.png"));
    }
    let family = clarp_speech::listen::strip(backend).to_lowercase();
    // Antigravity's pictures are filed under the model it runs.
    let family = match family.as_str() {
        "agy" => "gemini",
        family => family,
    };
    if !family.is_empty() {
        files.push(format!("default-{family}.png"));
    }
    files
}

/// The address of a picture: where it is fetched from, what it is called there, and what
/// is in it.
pub fn address(prefix: &str, name: &str, version: &str) -> String {
    format!(
        "{}/{}?v={version}",
        prefix.trim_end_matches('/'),
        quoted(name)
    )
}

/// A text as a part of an address, as Python's `urllib.parse.quote(text, safe="")` writes
/// it: letters, digits and `_.-~` as they are, every other byte as `%` and two capitals.
pub fn quoted(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            byte => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}
