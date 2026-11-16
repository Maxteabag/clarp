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

// ---- the face a push shows ---------------------------------------------------------------------

/// Where a phone fetches, without a token of its own, the picture an agent has of its own.
pub const FOR_NOTIFICATIONS: &str = "/notification-avatars";
/// For how long such an address is good, in seconds: a push service may hold a notification
/// while a phone is away.
pub const GOOD_FOR_S: i64 = 24 * 60 * 60;

/// HMAC-SHA256 (RFC 2104) of a message under a key, in hex.
pub fn hmac_sha256_hex(key: &[u8], message: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    const BLOCK: usize = 64;
    let mut padded = [0u8; BLOCK];
    if key.len() > BLOCK {
        padded[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        padded[..key.len()].copy_from_slice(key);
    }
    let inner: Vec<u8> = padded.iter().map(|byte| byte ^ 0x36).collect();
    let outer: Vec<u8> = padded.iter().map(|byte| byte ^ 0x5c).collect();
    let first = Sha256::new()
        .chain_update(&inner)
        .chain_update(message)
        .finalize();
    let second = Sha256::new()
        .chain_update(&outer)
        .chain_update(first)
        .finalize();
    second.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// What signs an address of [`FOR_NOTIFICATIONS`]: it holds for one agent, one picture and
/// until one moment, and only the Host that knows `secret` can have written it.
pub fn notification_signature(
    secret: &str,
    agent_id: &str,
    version: &str,
    expires_at: i64,
) -> String {
    let message = format!("{agent_id}\n{version}\n{expires_at}");
    hmac_sha256_hex(secret.as_bytes(), message.as_bytes())
}

/// Whether such an address is to be answered now (`now` in seconds): signed by this Host,
/// not run out, and not good for longer than this Host would have made it.
pub fn notification_authorized(
    secret: &str,
    (agent_id, version): (&str, &str),
    (expires_at, signature): (i64, &str),
    now: i64,
) -> bool {
    if secret.is_empty() || expires_at < now || expires_at > now + GOOD_FOR_S + 60 * 60 {
        return false;
    }
    let expected = notification_signature(secret, agent_id, version, expires_at);
    // Compared whole, whatever differs: how long it took says nothing of where.
    let (ours, theirs) = (expected.as_bytes(), signature.as_bytes());
    let differing = ours
        .iter()
        .zip(theirs)
        .fold(0u8, |seen, (a, b)| seen | (a ^ b));
    ours.len() == theirs.len() && differing == 0
}

/// The face a push shows of the agent it is about: its address for the phone that is told,
/// and whether it is a picture of the agent's own. `base` is where that phone reaches this
/// Host (empty: it never said, and no face is sent); `own` is what the address of the
/// agent's own picture carries; `shipped` the address of the picture this Host ships for it.
/// The agent's own picture is fetched by an address signed for a day; on a Host that asks
/// no token of anybody, by its plain address.
pub fn face_for_push(
    base: &str,
    (agent_id, own): (&str, Option<&str>),
    shipped: &str,
    (secret, now): (&str, i64),
) -> Option<(String, bool)> {
    if base.is_empty() {
        return None;
    }
    match own {
        Some(version) if secret.is_empty() => {
            let agent = quoted(agent_id);
            Some((format!("{base}/avatars/{agent}?v={version}"), true))
        }
        Some(version) => {
            let expires_at = now + GOOD_FOR_S;
            let signature = notification_signature(secret, agent_id, version, expires_at);
            let agent = quoted(agent_id);
            let address = format!(
                "{base}{FOR_NOTIFICATIONS}/{agent}?v={version}&exp={expires_at}&sig={signature}"
            );
            Some((address, true))
        }
        None if shipped.is_empty() => None,
        None => Some((format!("{base}{shipped}"), false)),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod push_tests {
    use super::*;

    #[test]
    fn a_face_for_a_push_is_signed_for_a_day_and_for_one_agent_and_one_picture() {
        // RFC 4231, test case 2; and two values Python's own `hmac` gives.
        assert_eq!(
            hmac_sha256_hex(b"Jefe", b"what do ya want for nothing?"),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
        assert_eq!(
            hmac_sha256_hex(&[b'k'; 100], b"x"),
            "8c1858bff6cf9cb73708b4b54f43bca6e1b8b4c3380760dc009001b265b64c6f"
        );
        let (agent, version, until) = ("agent-1", "f475387a7e87a1a9", 1_794_900_000);
        let signature = notification_signature("made-up-secret", agent, version, until);
        assert_eq!(
            signature,
            "2b42b72e0b06f2be5053d24c86041173bdc3fba56d9a4ea0bc416dfe55da9835"
        );
        let good = |secret: &str, who: (&str, &str), until: i64, signature: &str, now: i64| {
            notification_authorized(secret, who, (until, signature), now)
        };
        let now = until - 100;
        assert!(good(
            "made-up-secret",
            (agent, version),
            until,
            &signature,
            now
        ));
        // Another agent, another picture, another moment, another Host, a sign off: none.
        assert!(!good(
            "made-up-secret",
            ("agent-2", version),
            until,
            &signature,
            now
        ));
        assert!(!good(
            "made-up-secret",
            (agent, "0000000000000000"),
            until,
            &signature,
            now
        ));
        assert!(!good(
            "made-up-secret",
            (agent, version),
            until + 1,
            &signature,
            now
        ));
        assert!(!good(
            "another-secret",
            (agent, version),
            until,
            &signature,
            now
        ));
        let mut off = signature.clone();
        off.replace_range(0..1, "f");
        assert!(!good("made-up-secret", (agent, version), until, &off, now));
        assert!(!good(
            "made-up-secret",
            (agent, version),
            until,
            &signature[..63],
            now
        ));
        // Run out; good for longer than this Host makes one; and a Host without a secret.
        assert!(!good(
            "made-up-secret",
            (agent, version),
            until,
            &signature,
            until + 1
        ));
        let early = until - GOOD_FOR_S - 2 * 60 * 60;
        assert!(!good(
            "made-up-secret",
            (agent, version),
            until,
            &signature,
            early
        ));
        let unsigned = notification_signature("", agent, version, until);
        assert!(!good("", (agent, version), until, &unsigned, now));

        // What a push carries.
        let base = "https://host.example:7682";
        let pushed = |own: Option<&str>, shipped: &str, secret: &str| {
            face_for_push(base, ("agent 1", own), shipped, (secret, 1_794_813_600))
        };
        assert_eq!(
            pushed(
                Some(version),
                "/static/avatars/mike.png?v=1",
                "made-up-secret"
            ),
            Some((
                format!(
                    "{base}/notification-avatars/agent%201?v={version}&exp=1794900000&sig={}",
                    notification_signature("made-up-secret", "agent 1", version, 1_794_900_000)
                ),
                true
            ))
        );
        assert_eq!(
            pushed(Some(version), "", ""),
            Some((format!("{base}/avatars/agent%201?v={version}"), true))
        );
        assert_eq!(
            pushed(None, "/static/avatars/mike.png?v=1", "made-up-secret"),
            Some((format!("{base}/static/avatars/mike.png?v=1"), false))
        );
        assert_eq!(pushed(None, "", "made-up-secret"), None);
        let nowhere = face_for_push("", ("agent-1", Some(version)), "/x", ("s", 1));
        assert_eq!(
            nowhere, None,
            "a phone that never said where it reaches the Host"
        );
    }
}
