//! The two lists the apps fetch most carry a weak `ETag`, and a client that already holds
//! that answer is told so in a 304 with no body (issue 805, feature `list_etags`).
//!
//! Weak, because one tag names the answer plain and gzipped: the packer outside this layer
//! may send either, and both are the same list.
//!
//! On 25 Nov 2026 one phone fetched the snapshot 265 times in a day, nearly always the same.
//! The tag is a digest of the answer as it leaves the guards, with who reads it. It is not a
//! revision: both lists change with the clock as well as by events (an artifact expires, a
//! usage window resets, an agent's activity ages), and a tag that missed one of those would
//! tell an app its stale list is current. The digest costs one pass over bytes that are made
//! for the answer anyway, and spares the packing and the transfer of the answer itself.
//!
//! Per caller: the owner, each agent, an agent's tool that cannot say whose turn it is, and
//! another Host each get tags of their own (feature `bound_privacy`), and so does a device
//! paired with limited access, so a reader that is shown fewer agents or fewer fields never
//! holds a tag that also names the owner's answer, even on a day the two are byte for byte
//! the same.
//!
//! Measured on 25 Nov 2026 on a live Host of about 440 agents: neither list was the same
//! twice in ten fetches two seconds apart (agents' activity moves every second, and a plan
//! row tells the time it was read). The 304 saves the bytes and the packing on a quiet Host;
//! it does not spare building the answer.

use axum::body::{Body, HttpBody};
use axum::extract::Request;
use axum::http::{HeaderValue, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use sha2::{Digest, Sha256};

use crate::bound_privacy::Reader;

/// The routes whose answers are tagged: exactly these paths, any query.
const TAGGED: [&str; 2] = ["/agents/snapshot", "/artifacts"];

/// How a tagged answer may be kept: by the asking client only, and checked with the Host
/// before each use. An HTTP stack's cache that keeps the answer (not every one does: size
/// limits, a cache not configured) then sends the tag back by itself and hands its app the kept
/// body on a 304. Every other answer stays `no-store`.
const KEEP: &str = "private, no-cache";

/// Who an answer was made for, as the privacy guard judged the request: its reader, and
/// whether it reads as a device paired with limited access.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Caller {
    pub reader: Reader,
    pub limited: bool,
}

pub async fn layer(request: Request, next: Next) -> Response {
    if request.method() != Method::GET || !TAGGED.contains(&request.uri().path()) {
        return next.run(request).await;
    }
    let held = request
        .headers()
        .get_all(header::IF_NONE_MATCH)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .collect::<Vec<_>>()
        .join(",");
    let response = next.run(request).await;
    let whole = response.body().size_hint().exact().is_some();
    if response.status() != StatusCode::OK || !whole {
        return response;
    }
    let Some(caller) = response.extensions().get::<Caller>().cloned() else {
        // An answer no guard judged has no caller to tag it for: it goes untagged.
        return response;
    };
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, usize::MAX).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let hashed = bytes.clone();
    let Ok(tag) = tokio::task::spawn_blocking(move || tag(&caller, &hashed)).await else {
        return Response::from_parts(parts, Body::from(bytes));
    };
    if matches(&held, &tag) {
        return not_modified(&tag);
    }
    if let Ok(value) = HeaderValue::from_str(&format!("W/\"{tag}\"")) {
        parts.headers.insert(header::ETAG, value);
        parts
            .headers
            .insert(header::CACHE_CONTROL, HeaderValue::from_static(KEEP));
    }
    Response::from_parts(parts, Body::from(bytes))
}

/// The tag of an answer for its caller: 32 hex signs of a SHA-256 over who reads, with what
/// access, and what.
pub fn tag(caller: &Caller, body: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(if caller.limited {
        b"limited"
    } else {
        b"full\0\0\0"
    });
    match &caller.reader {
        Reader::Owner => digest.update(b"owner"),
        Reader::Agent {
            agent_id,
            home,
            provider,
        } => {
            for part in [
                b"agent".as_slice(),
                agent_id.as_bytes(),
                home.as_bytes(),
                provider.as_bytes(),
            ] {
                digest.update((part.len() as u64).to_be_bytes());
                digest.update(part);
            }
        }
        Reader::Unknown => digest.update(b"unknown"),
        Reader::Elsewhere => digest.update(b"elsewhere"),
    }
    digest.update([0u8]);
    digest.update(body);
    digest.finalize()[..16]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Whether an `If-None-Match` names this answer: `*`, or its tag, weak or strong (the
/// comparison `If-None-Match` asks for is the weak one).
fn matches(held: &str, tag: &str) -> bool {
    held.split(',').map(str::trim).any(|candidate| {
        let candidate = candidate.strip_prefix("W/").unwrap_or(candidate);
        candidate == "*" || candidate.trim_matches('"') == tag
    })
}

/// A 304: the tag, the headers that say how the answer may be kept, and no body.
fn not_modified(tag: &str) -> Response {
    let mut response = StatusCode::NOT_MODIFIED.into_response();
    let kept = response.headers_mut();
    if let Ok(value) = HeaderValue::from_str(&format!("W/\"{tag}\"")) {
        kept.insert(header::ETAG, value);
    }
    kept.insert(header::CACHE_CONTROL, HeaderValue::from_static(KEEP));
    // Both lists are JSON, which the packer varies by `Accept-Encoding`; it adds that to the
    // whole answer outside this layer, and leaves a 304 alone.
    kept.insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    response
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use axum::Router;
    use axum::routing::get;
    use tower::ServiceExt;

    fn big_json(rows: usize) -> String {
        let rows: Vec<String> = (0..rows)
            .map(|n| format!(r#"{{"session":"agent-{n}","title":"a row of the list"}}"#))
            .collect();
        format!(r#"{{"agents":[{}]}}"#, rows.join(","))
    }

    fn owner() -> Caller {
        Caller {
            reader: Reader::Owner,
            limited: false,
        }
    }

    /// The edge's order: the packer outside, the tagger, and a guard inside that names the
    /// caller as the real one does.
    fn router(caller: Caller, rows: usize) -> Router {
        let answer = move || {
            let caller = caller.clone();
            async move {
                let mut response =
                    ([(header::CONTENT_TYPE, "application/json")], big_json(rows)).into_response();
                response.extensions_mut().insert(caller);
                response
            }
        };
        Router::new()
            .route("/agents/snapshot", get(answer.clone()))
            .route("/other", get(answer))
            .layer(axum::middleware::from_fn(layer))
            .layer(axum::middleware::from_fn(crate::packed::layer))
    }

    async fn fetch(router: Router, path: &str, headers: &[(&str, &str)]) -> Response {
        let mut request = Request::builder().uri(path);
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        router
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap()
    }

    fn etag(response: &Response) -> String {
        response.headers()[header::ETAG]
            .to_str()
            .unwrap()
            .to_string()
    }

    #[tokio::test]
    async fn a_held_tag_is_answered_304_with_no_body() {
        let first = fetch(router(owner(), 50), "/agents/snapshot", &[]).await;
        assert_eq!(first.status(), StatusCode::OK);
        let tag = etag(&first);
        assert!(tag.starts_with("W/\"") && tag.len() == 36, "{tag}");
        assert_eq!(first.headers()[header::CACHE_CONTROL], KEEP);

        let again = fetch(
            router(owner(), 50),
            "/agents/snapshot",
            &[("if-none-match", tag.as_str())],
        )
        .await;
        assert_eq!(again.status(), StatusCode::NOT_MODIFIED);
        assert_eq!(etag(&again), tag);
        assert_eq!(again.headers()[header::CACHE_CONTROL], KEEP);
        let body = axum::body::to_bytes(again.into_body(), usize::MAX)
            .await
            .unwrap();
        assert!(body.is_empty());

        // A changed answer is sent whole, under another tag.
        let changed = fetch(
            router(owner(), 51),
            "/agents/snapshot",
            &[("if-none-match", tag.as_str())],
        )
        .await;
        assert_eq!(changed.status(), StatusCode::OK);
        assert_ne!(etag(&changed), tag);
    }

    #[tokio::test]
    async fn one_weak_tag_names_the_answer_plain_and_gzipped() {
        let plain = etag(&fetch(router(owner(), 50), "/agents/snapshot", &[]).await);
        let packed = fetch(
            router(owner(), 50),
            "/agents/snapshot",
            &[("accept-encoding", "gzip")],
        )
        .await;
        assert_eq!(packed.headers()[header::CONTENT_ENCODING], "gzip");
        assert_eq!(etag(&packed), plain);
        let strong = plain.trim_start_matches("W/").to_string();
        for held in [plain.clone(), strong] {
            let again = fetch(
                router(owner(), 50),
                "/agents/snapshot",
                &[
                    ("accept-encoding", "gzip"),
                    ("if-none-match", held.as_str()),
                ],
            )
            .await;
            assert_eq!(again.status(), StatusCode::NOT_MODIFIED, "{held}");
            assert_eq!(etag(&again), plain);
        }
    }

    #[tokio::test]
    async fn a_reader_of_fewer_agents_or_fields_never_shares_the_owners_tag() {
        let agent = Caller {
            reader: Reader::Agent {
                agent_id: "a1".into(),
                home: String::new(),
                provider: String::new(),
            },
            limited: false,
        };
        let limited = Caller {
            reader: Reader::Owner,
            limited: true,
        };
        let owner_tag = etag(&fetch(router(owner(), 50), "/agents/snapshot", &[]).await);
        for other in [agent, limited] {
            let answer = fetch(router(other.clone(), 50), "/agents/snapshot", &[]).await;
            assert_ne!(etag(&answer), owner_tag, "the same bytes, another caller");
            let held = fetch(
                router(other, 50),
                "/agents/snapshot",
                &[("if-none-match", owner_tag.as_str())],
            )
            .await;
            assert_eq!(held.status(), StatusCode::OK);
        }
        let by = |reader| Caller {
            reader,
            limited: false,
        };
        assert_ne!(
            tag(&by(Reader::Unknown), b"x"),
            tag(&by(Reader::Elsewhere), b"x")
        );
    }

    #[tokio::test]
    async fn another_route_is_not_tagged() {
        let response = fetch(router(owner(), 50), "/other", &[]).await;
        assert!(response.headers().get(header::ETAG).is_none());
        assert!(response.headers().get(header::CACHE_CONTROL).is_none());
    }
}
