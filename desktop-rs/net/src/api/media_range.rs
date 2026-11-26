//! Parts of the Host's media, read as a player asks for them: a byte range
//! of an artifact's file with the same credential, origin and redirect rules
//! as `get_bytes`, so an in-app player streams without ever holding the URL
//! or the token itself.

use reqwest::StatusCode;
use reqwest::header::{CONTENT_RANGE, RANGE};

use super::{ApiClient, auth_headers, http_status_text, same_origin};

/// A part of a media file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaRange {
    /// Where `bytes` begin in the file.
    pub start: u64,
    pub bytes: Vec<u8>,
    /// The whole file's length, when the Host said it.
    pub total: Option<u64>,
}

impl ApiClient {
    /// Bytes `start..start + len` of the Host's media at `path` (fewer at its
    /// end, none past it). A Host that ignores the range sends the whole file,
    /// which comes back as it is, starting at 0.
    pub async fn media_range(&self, path: &str, start: u64, len: u64) -> Result<MediaRange, String> {
        let (base, token) = self.snapshot();
        let url = self.resolve(path).filter(|url| base.as_ref().is_some_and(|b| same_origin(url, b)));
        let Some(url) = url else { return Err("Refusing cross-origin authenticated media".into()) };
        let last = start.saturating_add(len.max(1)) - 1;
        let response = self
            .media
            .get(url)
            .headers(auth_headers(&token, "video/*,audio/*,*/*"))
            .header(RANGE, format!("bytes={start}-{last}"))
            .send()
            .await
            .map_err(|error| error.without_url().to_string())?;
        let status = response.status();
        if status == StatusCode::RANGE_NOT_SATISFIABLE {
            let total = response.headers().get(CONTENT_RANGE).and_then(|v| v.to_str().ok()).and_then(total_of);
            return Ok(MediaRange { start, bytes: Vec::new(), total });
        }
        if !status.is_success() {
            return Err(http_status_text(status));
        }
        let partial = status == StatusCode::PARTIAL_CONTENT;
        let range = response.headers().get(CONTENT_RANGE).and_then(|v| v.to_str().ok()).map(str::to_owned);
        let bytes = response.bytes().await.map_err(|error| error.without_url().to_string())?.to_vec();
        if !partial {
            let total = bytes.len() as u64;
            return Ok(MediaRange { start: 0, bytes, total: Some(total) });
        }
        let begins = range.as_deref().and_then(first_of).unwrap_or(start);
        Ok(MediaRange { start: begins, bytes, total: range.as_deref().and_then(total_of) })
    }
}

/// `bytes 0-99/1234` -> 1234; `bytes */1234` -> 1234.
fn total_of(range: &str) -> Option<u64> {
    range.rsplit_once('/').and_then(|(_, total)| total.trim().parse().ok())
}

/// `bytes 100-199/1234` -> 100.
fn first_of(range: &str) -> Option<u64> {
    let spec = range.trim().strip_prefix("bytes")?.trim();
    spec.split_once('-').and_then(|(first, _)| first.trim().parse().ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_range_is_read() {
        assert_eq!(total_of("bytes 0-99/1234"), Some(1234));
        assert_eq!(total_of("bytes */1234"), Some(1234));
        assert_eq!(total_of("bytes 0-99/*"), None);
        assert_eq!(first_of("bytes 100-199/1234"), Some(100));
        assert_eq!(first_of("bytes */1234"), None);
    }
}
