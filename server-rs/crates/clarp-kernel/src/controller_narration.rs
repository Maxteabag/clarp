//! One spoken tutorial line, requested in a URL and cached by its synthesis inputs.
use sha2::{Digest, Sha256};

pub const PERSONA: &str = "Rachel";

pub fn text(query: &str) -> Option<String> {
    let raw = query.split('&').filter_map(|field| field.split_once('='))
        .find(|(key, _)| crate::media::unquoted(key) == "text")?.1;
    let printable: String = crate::media::unquoted(raw).chars()
        .filter(|&sign| crate::python::printed(sign)).collect();
    let line = printable.split_whitespace().collect::<Vec<_>>().join(" ");
    (!line.is_empty() && line.chars().count() <= 240).then_some(line)
}

pub fn cache_key(text: &str, voice: &str, model: &str) -> String {
    format!("{:x}", Sha256::digest(format!("v1\0{voice}\0{model}\0{text}").as_bytes()))
}
