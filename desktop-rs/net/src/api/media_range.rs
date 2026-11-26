//! Parts of the Host's media, read as a player asks for them: a byte range
//! of an artifact's file with the same credential, origin and redirect rules
//! as `get_bytes`, so an in-app player streams without ever holding the URL
//! or the token itself.

use std::time::Duration;

use reqwest::StatusCode;
use reqwest::header::{CONTENT_RANGE, RANGE};

use super::{ApiClient, auth_headers, http_status_text, same_origin};

/// How long one part may take, start to last byte: a read never hangs.
const PART_TIMEOUT: Duration = Duration::from_secs(30);

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
            .timeout(PART_TIMEOUT)
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
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// A one-file Host: answers each request with what `answer` says for
    /// its request line and headers, and keeps every request's head.
    async fn host(answer: fn(&str, u16) -> String) -> (u16, Arc<Mutex<Vec<String>>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let seen: Arc<Mutex<Vec<String>>> = Arc::default();
        let kept = seen.clone();
        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                let kept = kept.clone();
                tokio::spawn(async move {
                    let mut head = Vec::new();
                    let mut byte = [0u8; 1];
                    while !head.ends_with(b"\r\n\r\n") && socket.read(&mut byte).await.is_ok_and(|n| n == 1) {
                        head.push(byte[0]);
                    }
                    let head = String::from_utf8_lossy(&head).to_string();
                    kept.lock().unwrap().push(head.clone());
                    let _ = socket.write_all(answer(&head, port).as_bytes()).await;
                });
            }
        });
        (port, seen)
    }

    fn client(port: u16) -> ApiClient {
        let api = ApiClient::new(tokio::runtime::Handle::current(), |_| {});
        api.set_endpoint(url::Url::parse(&format!("http://127.0.0.1:{port}")).unwrap(), "secret-token");
        api
    }

    const FILE: &[u8] = b"0123456789abcdefghij";

    /// The Host's own answers (server-rs media.rs): 206 with Content-Range,
    /// 416 `bytes */total` past the end.
    fn ranged(head: &str, _port: u16) -> String {
        let range = head.lines().find_map(|l| l.to_ascii_lowercase().strip_prefix("range: bytes=").map(str::to_owned)).unwrap_or_default();
        let (first, last) = range.split_once('-').map(|(a, b)| (a.parse::<usize>().unwrap(), b.trim().parse::<usize>().unwrap())).unwrap();
        if first >= FILE.len() {
            return format!("HTTP/1.1 416 Range Not Satisfiable\r\ncontent-range: bytes */{}\r\ncontent-length: 0\r\n\r\n", FILE.len());
        }
        let last = last.min(FILE.len() - 1);
        let body = String::from_utf8_lossy(&FILE[first..=last]).to_string();
        format!("HTTP/1.1 206 Partial Content\r\ncontent-range: bytes {first}-{last}/{}\r\ncontent-length: {}\r\n\r\n{body}", FILE.len(), body.len())
    }

    #[tokio::test]
    async fn parts_and_the_end() {
        let (port, seen) = host(ranged).await;
        let api = client(port);
        let part = api.media_range("/media/a", 5, 4).await.expect("part");
        assert_eq!((part.start, part.bytes.as_slice(), part.total), (5, &b"5678"[..], Some(20)));
        let tail = api.media_range("/media/a", 18, 1 << 20).await.expect("tail");
        assert_eq!((tail.start, tail.bytes.as_slice(), tail.total), (18, &b"ij"[..], Some(20)));
        let past = api.media_range("/media/a", 20, 10).await.expect("past the end");
        assert!(past.bytes.is_empty() && past.total == Some(20));
        let heads = seen.lock().unwrap().clone();
        assert!(heads[0].to_ascii_lowercase().contains("range: bytes=5-8"), "{heads:?}");
        assert!(heads.iter().all(|h| h.contains("Bearer secret-token")), "every part carries the token");
    }

    fn elsewhere(head: &str, port: u16) -> String {
        if head.starts_with("GET /media/moved ") {
            // Another origin: the same machine by another name.
            format!("HTTP/1.1 302 Found\r\nlocation: http://localhost:{port}/media/stolen\r\ncontent-length: 0\r\n\r\n")
        } else if head.starts_with("GET /media/here ") {
            "HTTP/1.1 302 Found\r\nlocation: /media/a\r\ncontent-length: 0\r\n\r\n".into()
        } else {
            ranged(head, port)
        }
    }

    #[tokio::test]
    async fn redirects_keep_the_token_home() {
        let (port, seen) = host(elsewhere).await;
        let api = client(port);
        assert!(api.media_range("/media/moved", 0, 4).await.is_err(), "a redirect to another origin is refused");
        let heads = seen.lock().unwrap().clone();
        assert!(!heads.iter().any(|h| h.starts_with("GET /media/stolen")), "nothing is asked of the other origin: {heads:?}");
        let part = api.media_range("/media/here", 0, 4).await.expect("same-origin redirect");
        assert_eq!(part.bytes, b"0123");
        assert!(api.media_range("http://localhost:1/media/a", 0, 4).await.is_err(), "another origin's URL is refused outright");
    }

    #[test]
    fn content_range_is_read() {
        assert_eq!(total_of("bytes 0-99/1234"), Some(1234));
        assert_eq!(total_of("bytes */1234"), Some(1234));
        assert_eq!(total_of("bytes 0-99/*"), None);
        assert_eq!(first_of("bytes 100-199/1234"), Some(100));
        assert_eq!(first_of("bytes */1234"), None);
    }
}
