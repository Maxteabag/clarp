//! Identifiers the Host mints: random, and never meaningful.

use std::io::Read;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static FALLBACK: AtomicU64 = AtomicU64::new(0);

/// `count` random bytes, from the system. If the system has none to give, bytes that at least
/// differ from call to call.
fn random(count: usize) -> Vec<u8> {
    let mut bytes = vec![0u8; count];
    let read =
        std::fs::File::open("/dev/urandom").and_then(|mut source| source.read_exact(&mut bytes));
    if read.is_err() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let mut state = (nanos ^ FALLBACK.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::Relaxed)) | 1;
        for byte in &mut bytes {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            *byte = (state >> 24) as u8;
        }
    }
    bytes
}

/// `count` random bytes as lowercase hex.
pub fn hex(count: usize) -> String {
    random(count)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A random identifier in the usual 8-4-4-4-12 form.
pub fn uuid() -> String {
    let h = hex(16);
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

/// A name as it may appear in a session: lowercase letters, digits and dashes.
pub fn slug(name: &str) -> String {
    let mut slug = String::new();
    for ch in name.trim().chars() {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch.to_ascii_lowercase());
        } else if !slug.ends_with('-') && !slug.is_empty() {
            slug.push('-');
        }
    }
    let slug = slug.trim_end_matches('-').to_string();
    if slug.is_empty() {
        "agent".to_string()
    } else {
        slug
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_have_their_form_and_differ() {
        assert_eq!(hex(8).len(), 16);
        assert!(hex(8).chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(hex(8), hex(8));
        let id = uuid();
        assert_eq!(id.len(), 36);
        assert_eq!(id.matches('-').count(), 4);
    }

    #[test]
    fn a_name_becomes_a_session_name() {
        assert_eq!(slug("Maximus"), "maximus");
        assert_eq!(slug("  Rachel the 2nd! "), "rachel-the-2nd");
        assert_eq!(slug("ÆØÅ"), "agent");
        assert_eq!(slug("a--b"), "a-b");
    }
}
