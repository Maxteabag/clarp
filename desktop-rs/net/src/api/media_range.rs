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
/// The most one part may ask for: what a read keeps in memory.
pub const MAX_PART: u64 = 8 << 20;
/// A Host that ignores the range sends the file from its start: this much
/// of it is read past (and dropped) at most to reach the part.
const MAX_SKIP: u64 = 64 << 20;

/// A part of a media file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaRange {
    /// Where `bytes` begin in the file (always where it was asked).
    pub start: u64,
    pub bytes: Vec<u8>,
    /// The whole file's length, when the Host said it.
    pub total: Option<u64>,
}

impl ApiClient {
    /// Bytes `start..start + len` of the Host's media at `path` (fewer at its
    /// end, none past it), `len` at most `MAX_PART`. The body is read in
    /// chunks and never past what was asked, whatever the Host says it sends:
    /// a part longer than asked, or one that begins elsewhere, is refused. A
    /// Host that ignores the range (200) is read past up to the part, within
    /// `MAX_SKIP`.
    pub async fn media_range(&self, path: &str, start: u64, len: u64) -> Result<MediaRange, String> {
        let len = len.clamp(1, MAX_PART);
        let (base, token) = self.snapshot();
        let url = self.resolve(path).filter(|url| base.as_ref().is_some_and(|b| same_origin(url, b)));
        let Some(url) = url else { return Err("Refusing cross-origin authenticated media".into()) };
        let last = start.saturating_add(len) - 1;
        let mut response = self
            .media
            .get(url)
            .headers(auth_headers(&token, "video/*,audio/*,*/*"))
            .header(RANGE, format!("bytes={start}-{last}"))
            .timeout(PART_TIMEOUT)
            .send()
            .await
            .map_err(|error| error.without_url().to_string())?;
        let status = response.status();
        let content_range = response.headers().get(CONTENT_RANGE).and_then(|v| v.to_str().ok()).map(str::to_owned);
        if status == StatusCode::RANGE_NOT_SATISFIABLE {
            return Ok(MediaRange { start, bytes: Vec::new(), total: content_range.as_deref().and_then(total_of) });
        }
        if !status.is_success() {
            return Err(http_status_text(status));
        }
        if status == StatusCode::PARTIAL_CONTENT {
            let (first, end, total) = content_range.as_deref().and_then(span_of).ok_or("The Host's part has no usable Content-Range")?;
            if first != start || end < first || end > last || total.is_some_and(|t| end >= t) {
                return Err(format!("The Host sent another part than asked (bytes {first}-{end} for {start}-{last})"));
            }
            let want = end - first + 1;
            let bytes = read_body(&mut response, 0, want).await?;
            if (bytes.len() as u64) != want {
                return Err("The Host's part ended early".into());
            }
            return Ok(MediaRange { start, bytes, total });
        }
        // The whole file, from its start: read past what comes before the part.
        if start > MAX_SKIP {
            return Err("The Host doesn't send parts of its media".into());
        }
        let total = response.content_length();
        let bytes = read_body(&mut response, start, len).await?;
        Ok(MediaRange { start, bytes, total })
    }
}

/// Reads the body in chunks: drops the first `skip` bytes, keeps at most
/// `keep` after them and stops reading there, so a Host can never make
/// this hold more than `keep` (nor read more than `skip + keep`).
async fn read_body(response: &mut reqwest::Response, skip: u64, keep: u64) -> Result<Vec<u8>, String> {
    let mut kept = Vec::with_capacity(keep.min(MAX_PART) as usize);
    let mut seen: u64 = 0;
    while (kept.len() as u64) < keep {
        let Some(chunk) = response.chunk().await.map_err(|error| error.without_url().to_string())? else { break };
        let from = skip.saturating_sub(seen).min(chunk.len() as u64) as usize;
        seen += chunk.len() as u64;
        let room = (keep - kept.len() as u64) as usize;
        let take = &chunk[from..];
        kept.extend_from_slice(&take[..take.len().min(room)]);
    }
    Ok(kept)
}

/// `bytes 0-99/1234` -> 1234; `bytes */1234` -> 1234.
fn total_of(range: &str) -> Option<u64> {
    range.rsplit_once('/').and_then(|(_, total)| total.trim().parse().ok())
}

/// `bytes 100-199/1234` -> (100, 199, Some(1234)); `/*` -> no total.
fn span_of(range: &str) -> Option<(u64, u64, Option<u64>)> {
    let spec = range.trim().strip_prefix("bytes")?.trim();
    let (span, total) = spec.split_once('/')?;
    let (first, end) = span.split_once('-')?;
    let total = match total.trim() {
        "*" => None,
        digits => Some(digits.parse().ok()?),
    };
    Some((first.trim().parse().ok()?, end.trim().parse().ok()?, total))
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
        assert_eq!(span_of("bytes 100-199/1234"), Some((100, 199, Some(1234))));
        assert_eq!(span_of("bytes 100-199/*"), Some((100, 199, None)));
        assert_eq!(span_of("bytes */1234"), None);
    }

    /// A Host that answers a 4-byte ask with a part far longer than asked.
    fn overlong(_head: &str, _port: u16) -> String {
        let body = "x".repeat(1 << 20);
        format!("HTTP/1.1 206 Partial Content\r\ncontent-range: bytes 0-{}/{}\r\ncontent-length: {}\r\n\r\n{body}", body.len() - 1, body.len(), body.len())
    }

    /// One that says it sends the asked part, then sends far more.
    fn lying(_head: &str, _port: u16) -> String {
        format!("HTTP/1.1 206 Partial Content\r\ncontent-range: bytes 0-3/100\r\n\r\n{}", "y".repeat(1 << 20))
    }

    /// An old Host: ignores Range and sends a large whole file, no length.
    fn whole(_head: &str, _port: u16) -> String {
        let mut body = String::from("0123456789");
        body.push_str(&"z".repeat(4 << 20));
        format!("HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n{body}")
    }

    #[tokio::test]
    async fn a_host_cannot_make_it_read_more() {
        let (port, _) = host(overlong).await;
        let error = client(port).media_range("/media/a", 0, 4).await.expect_err("a longer part is refused");
        assert!(error.contains("another part"), "{error}");
        let (port, _) = host(lying).await;
        let part = client(port).media_range("/media/a", 0, 4).await.expect("the asked part");
        assert_eq!(part.bytes, b"yyyy", "only what was asked is kept");
        let (port, _) = host(whole).await;
        let part = client(port).media_range("/media/a", 3, 4).await.expect("an old Host's whole file");
        assert_eq!((part.start, part.bytes.as_slice(), part.total), (3, &b"3456"[..], None), "read past to the part, kept only it");
        let error = client(port).media_range("/media/a", MAX_SKIP + 1, 4).await.expect_err("too far into a whole file");
        assert!(error.contains("parts"), "{error}");
    }

    #[tokio::test]
    async fn a_part_is_never_larger_than_the_limit() {
        let (port, seen) = host(ranged).await;
        let _ = client(port).media_range("/media/a", 0, u64::MAX).await.expect("part");
        let head = seen.lock().unwrap()[0].to_ascii_lowercase();
        assert!(head.contains(&format!("range: bytes=0-{}", MAX_PART - 1)), "{head}");
    }
}
