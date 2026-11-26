//! What the Host answers whole, it sends gzipped to a client that takes gzip (issue 805).
//!
//! On 25 Nov 2026 a Host of about 310 agents answered the snapshot in about 1.25 MB and the
//! artifact list in about 8.6 MB, the same bytes whatever the phone said it takes; one phone
//! fetched over 400 MB of them in a day. The Python Host gzipped the same answers (its
//! `_send`: JSON and text of 1 KB and more, level 5), and the relay connector already asks
//! for gzip where the relay passes a packed answer through untouched.
//!
//! An answer is packed only when it is whole: its size is known before it is sent. A stream
//! (events, a terminal, a file read in parts) has no known size and goes as it comes, so its
//! framing never waits for a packer. A part of something (a range), a socket's upgrade, an
//! answer already packed, an answer with no body (`HEAD`, 204, 304) and media, which is
//! packed by its own format, go as they are.

use axum::body::{Body, HttpBody};
use axum::extract::Request;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use flate2::{Compression, write::GzEncoder};
use std::io::Write;

/// Smaller than this, the gzip header and the time cost more than they save.
pub const SMALLEST: u64 = 1024;
/// Larger than this, an answer goes plain: a body of this size is no list an app reads, and
/// packing it would hold a worker thread for long.
pub const LARGEST: u64 = 64 << 20;
/// The Python Host's level: about as small as level 9 for JSON at a third of the time.
const LEVEL: u32 = 5;
/// Above this, level 1. Measured on 25 Nov 2026: this packer took about 1.2 s of CPU for a
/// 16 MB list at level 5, about 75 ms a MB. On a real list of 7.4 MB, level 1 packs to 34%
/// where level 5 packs to 27%, in less than half the time; a snapshot of 1.25 MB stays at
/// level 5 (11%).
const FAST_ABOVE: usize = 2 << 20;

/// Whether a request takes its answer gzipped: one with a body, whole, from a client that
/// takes gzip.
pub fn asked(method: &Method, headers: &HeaderMap) -> bool {
    method != Method::HEAD
        && !headers.contains_key(header::RANGE)
        && headers
            .get(header::ACCEPT_ENCODING)
            .and_then(|value| value.to_str().ok())
            .is_some_and(clarp_kernel::connector::accepts_gzip)
}

pub async fn layer(request: Request, next: Next) -> Response {
    let asked = asked(request.method(), request.headers());
    let streamed = asked && stream_asked(request.uri().query());
    let response = next.run(request).await;
    if streamed && event_stream(response.status(), response.headers()) {
        return packed_stream(response);
    }
    if !packable(response.status(), response.headers(), response.body()) {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    // What a cache keeps of it depends on what was asked, also when it goes plain.
    parts
        .headers
        .append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    if !asked {
        return Response::from_parts(parts, body);
    }
    // The size is known, so the body is already in memory: this reads no stream.
    let Ok(plain) = axum::body::to_bytes(body, usize::MAX).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    // Packing a large list is work for a worker thread, not for the runtime's.
    let packing = plain.clone();
    let packed = tokio::task::spawn_blocking(move || gzip(&packing))
        .await
        .ok()
        .flatten();
    match packed {
        Some(packed) if packed.len() < plain.len() => {
            parts.headers.remove(header::CONTENT_LENGTH);
            parts
                .headers
                .insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
            Response::from_parts(parts, Body::from(packed))
        }
        _ => Response::from_parts(parts, Body::from(plain)),
    }
}

/// Whether a request for a stream of events asks for it gzipped as well (`gzip=1`, feature
/// `events_slim`, issue 879). An app that does not ask is sent the stream plain, whatever its
/// HTTP stack says it takes: an older app reads it as it always has.
fn stream_asked(query: Option<&str>) -> bool {
    url::form_urlencoded::parse(query.unwrap_or("").as_bytes())
        .any(|(key, value)| key == "gzip" && matches!(value.trim(), "1" | "true" | "yes"))
}

/// Whether an answer is a stream of events that is sent as it is made.
fn event_stream(status: StatusCode, headers: &HeaderMap) -> bool {
    status == StatusCode::OK
        && !headers.contains_key(header::CONTENT_ENCODING)
        && headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|kind| {
                kind.trim_start()
                    .to_ascii_lowercase()
                    .starts_with("text/event-stream")
            })
}

/// A stream of events gzipped as one gzip stream, each part flushed as it comes, so that an
/// event is never held back for the next: the client decodes each event when it arrives, and
/// what the events repeat (their keys, the agents' names) is sent once.
fn packed_stream(response: Response) -> Response {
    let (mut parts, body) = response.into_parts();
    parts.headers.remove(header::CONTENT_LENGTH);
    parts
        .headers
        .insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
    parts
        .headers
        .append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    let encoder = GzEncoder::new(Vec::new(), Compression::new(LEVEL));
    let parts_of = futures_util::stream::unfold(
        (body.into_data_stream(), Some(encoder)),
        |(mut inner, encoder)| async move {
            use futures_util::StreamExt;
            let mut encoder = encoder?;
            loop {
                match inner.next().await {
                    Some(Ok(part)) => {
                        let packed = encoder.write_all(&part).and_then(|()| encoder.flush());
                        if packed.is_err() {
                            return None;
                        }
                        let out = std::mem::take(encoder.get_mut());
                        if out.is_empty() {
                            continue;
                        }
                        let out = axum::body::Bytes::from(out);
                        return Some((Ok::<_, axum::Error>(out), (inner, Some(encoder))));
                    }
                    Some(Err(why)) => return Some((Err(why), (inner, None))),
                    // The stream ended: the gzip trailer, and nothing after it.
                    None => {
                        let rest = encoder.finish().ok()?;
                        let rest = axum::body::Bytes::from(rest);
                        return Some((Ok(rest), (inner, None)));
                    }
                }
            }
        },
    );
    Response::from_parts(parts, Body::from_stream(parts_of))
}

/// Whether an answer is one the Host packs for a client that takes gzip.
pub fn packable(status: StatusCode, headers: &HeaderMap, body: &Body) -> bool {
    let whole = body
        .size_hint()
        .exact()
        .is_some_and(|size| (SMALLEST..=LARGEST).contains(&size));
    let bodiless = status.is_informational()
        || matches!(
            status,
            StatusCode::NO_CONTENT | StatusCode::PARTIAL_CONTENT | StatusCode::NOT_MODIFIED
        );
    whole
        && !bodiless
        && !headers.contains_key(header::CONTENT_ENCODING)
        && !headers.contains_key(header::CONTENT_RANGE)
        && headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(text)
}

/// The kinds the Python Host packs: JSON, text and scripts. An event stream is text but no
/// answer that ends; it is refused here as well as by its unknown size.
fn text(content_type: &str) -> bool {
    let kind = content_type.trim_start().to_ascii_lowercase();
    !kind.starts_with("text/event-stream")
        && (kind.starts_with("application/json")
            || kind.starts_with("text/")
            || kind.starts_with("application/javascript"))
}

pub(crate) fn gzip(plain: &[u8]) -> Option<Vec<u8>> {
    let level = if plain.len() > FAST_ABOVE { 1 } else { LEVEL };
    let mut encoder = GzEncoder::new(Vec::with_capacity(plain.len() / 4), Compression::new(level));
    encoder.write_all(plain).ok()?;
    encoder.finish().ok()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use axum::Router;
    use axum::routing::get;
    use flate2::read::GzDecoder;
    use std::io::Read;
    use tower::ServiceExt;

    fn big_json() -> String {
        let rows: Vec<String> = (0..400)
            .map(|n| {
                format!(r#"{{"session":"agent-{n}","busy":false,"title":"a row of the list"}}"#)
            })
            .collect();
        format!(r#"{{"agents":[{}]}}"#, rows.join(","))
    }

    fn router() -> Router {
        let json = big_json();
        let events = || async {
            let parts = futures_util::stream::iter(
                (0..50)
                    .map(|n| Ok::<_, std::io::Error>(format!("data: {n} {}\n\n", "x".repeat(80)))),
            );
            Response::builder()
                .header(header::CONTENT_TYPE, "text/event-stream")
                .body(Body::from_stream(parts))
                .unwrap()
        };
        let picture = || async {
            Response::builder()
                .header(header::CONTENT_TYPE, "image/png")
                .body(Body::from(vec![7u8; 4096]))
                .unwrap()
        };
        let ranged = || async {
            Response::builder()
                .status(StatusCode::PARTIAL_CONTENT)
                .header(header::CONTENT_TYPE, "text/plain")
                .header(header::CONTENT_RANGE, "bytes 0-4095/9000")
                .body(Body::from("y".repeat(4096)))
                .unwrap()
        };
        let small = || async { axum::Json(serde_json::json!({"ok": true})) };
        Router::new()
            .route(
                "/big",
                get(move || {
                    let json = json.clone();
                    async move { ([(header::CONTENT_TYPE, "application/json")], json) }
                }),
            )
            .route("/events", get(events))
            .route("/picture", get(picture))
            .route("/ranged", get(ranged))
            .route("/small", get(small))
            .layer(axum::middleware::from_fn(layer))
    }

    async fn fetch(path: &str, accept: Option<&str>) -> (Response, Vec<u8>) {
        let mut request = Request::builder().uri(path);
        if let Some(accept) = accept {
            request = request.header(header::ACCEPT_ENCODING, accept);
        }
        let response = router()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let (parts, body) = response.into_parts();
        let bytes = axum::body::to_bytes(body, usize::MAX).await.unwrap();
        (Response::from_parts(parts, Body::empty()), bytes.to_vec())
    }

    fn encoding(response: &Response) -> Option<&str> {
        response
            .headers()
            .get(header::CONTENT_ENCODING)
            .and_then(|value| value.to_str().ok())
    }

    #[tokio::test]
    async fn a_large_json_answer_is_gzipped_when_asked_and_decodes_to_the_plain_one() {
        let (plain_response, plain) = fetch("/big", None).await;
        assert_eq!(encoding(&plain_response), None);
        assert_eq!(plain, big_json().into_bytes());
        assert_eq!(
            plain_response.headers().get(header::VARY).unwrap(),
            "Accept-Encoding"
        );

        let (packed_response, packed) = fetch("/big", Some("gzip, deflate, br")).await;
        assert_eq!(encoding(&packed_response), Some("gzip"));
        assert_eq!(
            packed_response.headers().get(header::VARY).unwrap(),
            "Accept-Encoding"
        );
        assert!(
            packed.len() * 4 < plain.len(),
            "{} of {}",
            packed.len(),
            plain.len()
        );
        let mut decoded = Vec::new();
        GzDecoder::new(&packed[..])
            .read_to_end(&mut decoded)
            .unwrap();
        assert_eq!(decoded, plain);
    }

    #[tokio::test]
    async fn gzip_weighed_at_nothing_is_not_taken() {
        let (response, body) = fetch("/big", Some("gzip;q=0, identity")).await;
        assert_eq!(encoding(&response), None);
        assert_eq!(body, big_json().into_bytes());
    }

    #[tokio::test]
    async fn a_stream_is_never_packed() {
        let (response, body) = fetch("/events", Some("gzip")).await;
        assert_eq!(encoding(&response), None);
        assert!(response.headers().get(header::VARY).is_none());
        assert!(body.starts_with(b"data: 0 "));
    }

    #[tokio::test]
    async fn a_stream_that_asks_is_gzipped_event_by_event() {
        // Without gzip in the header, the word alone packs nothing.
        let (response, body) = fetch("/events?gzip=1", None).await;
        assert_eq!(encoding(&response), None);
        assert!(body.starts_with(b"data: 0 "));

        let (response, packed) = fetch("/events?live=a&gzip=1", Some("gzip")).await;
        assert_eq!(encoding(&response), Some("gzip"));
        assert_eq!(
            response.headers().get(header::VARY).unwrap(),
            "Accept-Encoding"
        );
        let mut decoded = Vec::new();
        GzDecoder::new(&packed[..])
            .read_to_end(&mut decoded)
            .unwrap();
        let plain: String = (0..50)
            .map(|n| format!("data: {n} {}\n\n", "x".repeat(80)))
            .collect();
        assert_eq!(decoded, plain.into_bytes());
        assert!(
            packed.len() * 3 < decoded.len(),
            "{} of {}",
            packed.len(),
            decoded.len()
        );
    }

    #[tokio::test]
    async fn each_event_of_a_packed_stream_can_be_read_when_it_comes() {
        let request = Request::builder()
            .uri("/events?gzip=1")
            .header(header::ACCEPT_ENCODING, "gzip")
            .body(Body::empty())
            .unwrap();
        let response = router().oneshot(request).await.unwrap();
        let mut parts = response.into_body().into_data_stream();
        let mut decoder = flate2::write::GzDecoder::new(Vec::new());
        use futures_util::StreamExt;
        use std::io::Write;
        for n in 0..3 {
            let part = parts.next().await.unwrap().unwrap();
            decoder.write_all(&part).unwrap();
            decoder.flush().unwrap();
            let read = String::from_utf8(std::mem::take(decoder.get_mut())).unwrap();
            assert_eq!(
                read,
                format!("data: {n} {}\n\n", "x".repeat(80)),
                "event {n} whole on its own"
            );
        }
    }

    #[tokio::test]
    async fn media_a_range_and_a_small_answer_go_as_they_are() {
        for path in ["/picture", "/ranged", "/small"] {
            let (response, _) = fetch(path, Some("gzip")).await;
            assert_eq!(encoding(&response), None, "{path}");
        }
    }

    #[tokio::test]
    async fn a_request_for_a_range_is_answered_plain() {
        let request = Request::builder()
            .uri("/big")
            .header(header::ACCEPT_ENCODING, "gzip")
            .header(header::RANGE, "bytes=0-10")
            .body(Body::empty())
            .unwrap();
        let response = router().oneshot(request).await.unwrap();
        assert_eq!(encoding(&response), None);
    }
}
