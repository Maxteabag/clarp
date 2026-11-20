//! Rules for edits of the agent's primary portrait requested by its owner.
use crate::contacts::picture;
use crate::kept_portraits::picture_kind;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const KIND: &str = "portrait-generation";
pub const MAX_IMAGE: usize = 20 * 1024 * 1024;
pub const MAX_RESPONSE: usize = 32 * 1024 * 1024;
pub const DAILY_REASON: &str =
    "This Computer has begun 20 portrait generations in the last 24 hours";
pub const PROMPTS: [&str; 2] = [
    concat!(
        "Preserve the exact identity, facial features, hair, age, visual style, and ",
        "character design from the reference. Create an intimate, warm close-up ",
        "portrait that feels candid and personal, showing more of this character's ",
        "life. Keep one character only. No text, logos, frames, or watermarks."
    ),
    concat!(
        "Preserve the exact identity, facial features, hair, age, visual style, and ",
        "character design from the reference. Create a natural everyday-life scene ",
        "with the same character in a meaningful environment, emotionally warm and ",
        "personal. Keep one character only. No text, logos, frames, or watermarks."
    ),
];

pub fn job_id(agent: &str) -> String {
    let digest = format!("{:x}", Sha256::digest(agent.as_bytes()));
    format!("portrait-generation-{}", &digest[..20])
}

pub fn supported(mime: &str) -> bool {
    matches!(mime, "image/png" | "image/jpeg" | "image/webp")
}

pub fn capability(
    known: bool,
    key: bool,
    primary: Option<(&str, u64)>,
    job: Value,
    begun: u32,
) -> Value {
    let active = matches!(job["status"].as_str(), Some("queued" | "running"));
    let reason = if !known {
        "Unknown Agent session"
    } else if !key {
        "Configure an OpenAI API key on this Computer"
    } else {
        match primary {
            None => "Add or select a primary portrait first",
            Some((mime, _)) if !supported(mime) => "Use a PNG, JPEG, or WebP primary portrait",
            Some((_, len)) if len > MAX_IMAGE as u64 => {
                "Primary portrait is too large to generate from"
            }
            // An active duplicate starts no work, even when it was the twentieth launch.
            _ if !active && picture::admitted(None, begun).is_none() => DAILY_REASON,
            _ => "",
        }
    };
    json!({"contract":"portrait-generation.v1", "available":reason.is_empty(),"provider":"openai",
           "model":picture::MODEL,"count":2,"cost_notice":"Uses the configured OpenAI API account; image charges may apply.",
           "reason":reason,"job":job})
}

/// The provider accepts one reference image per request. All body fields precede the binary part.
pub fn multipart(boundary: &str, prompt: &str, source: &[u8], mime: &str) -> Vec<u8> {
    let mut body = Vec::with_capacity(source.len() + prompt.len() + 1024);
    for (field, value) in [
        ("model", picture::MODEL),
        ("prompt", prompt),
        ("size", "1024x1024"),
        ("quality", "medium"),
    ] {
        body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n").as_bytes());
    }
    body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"portrait.png\"\r\nContent-Type: {mime}\r\n\r\n").as_bytes());
    body.extend_from_slice(source);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    body
}

pub fn generated_kind(bytes: &[u8]) -> Result<&'static str, &'static str> {
    if bytes.len() <= MAX_IMAGE
        && let Some(kind) = picture_kind(bytes).filter(|kind| supported(kind))
    {
        return Ok(kind);
    }
    Err("image provider returned invalid portrait content")
}

pub fn distinct(first: &[u8], second: &[u8]) -> Result<(), &'static str> {
    if first == second {
        Err("image provider returned duplicate portrait alternatives")
    } else {
        Ok(())
    }
}
