//! The snapshot and the artifact list carry a weak tag for their caller through the Host's own
//! layers (issue 805, feature `list_etags`): a held tag is answered 304 with no body, the same
//! tag names the answer gzipped, and an agent's turn and another Host are given tags of their
//! own, so neither can be answered 304 with the owner's tag.

use super::*;
use crate::bound_privacy::{agent_id, first_turn_token};
use flate2::read::GzDecoder;

const KEEP: &str = "private, no-cache";

#[test]
fn the_snapshot_and_the_artifact_list_are_tagged_for_their_caller_through_the_hosts_layers() {
    let place = Place::new("list-etags");
    // With a token: a Host without one is open to this machine only, and refuses whatever a
    // proxy hands on.
    let host = place.start_with_token("the-administrators-token");
    let ben = host.new_agent("Ben");
    host.send(&ben, "hello", "u-1");
    wait_for("the first turn", || host.state(&ben) == "done");
    // A document long enough that the list is packed.
    let document = json!({
        "session": ben, "type": "document", "title": "A report",
        "summary": "What it found", "payload": {"content": "words of the report ".repeat(200)},
    });
    let (status, made) = host.ask("POST", "/artifacts", Some(&document));
    assert_eq!(status, 201, "{made}");
    let turn = first_turn_token(&place, &agent_id(&host, &ben));
    let get =
        |path: &str, headers: &[(&str, &str)]| fetch(&host, &host.token, "GET", path, headers, b"");
    // A paired phone: a proxy on this machine (Tailscale Serve) may hand its requests on, where
    // the owner's token is taken only from this machine itself.
    let (phone, _) = pair(&host, "Phone", "full");
    let from_phone =
        |path: &str, headers: &[(&str, &str)]| fetch(&host, &phone, "GET", path, headers, b"");

    for path in ["/agents/snapshot", "/artifacts"] {
        let first = get(path, &[]);
        assert_eq!(first.status, 200, "{path}: {}", first.text());
        let tag = first.header("etag").to_string();
        assert!(
            tag.starts_with("W/\"") && tag.len() == 36,
            "{path}: {tag:?}"
        );
        assert_eq!(first.header("cache-control"), KEEP, "{path}");

        // The tag held: 304, no body, the same tag and the same keeping.
        let held = get(path, &[("If-None-Match", &tag)]);
        assert_eq!(held.status, 304, "{path}: {}", held.text());
        assert!(held.body.is_empty(), "{path}");
        assert_eq!(held.header("etag"), tag, "{path}");
        assert_eq!(held.header("cache-control"), KEEP, "{path}");

        // From this machine a client that takes plain is sent it plain (issue 932): the same
        // tag, the same bytes, and still told that the answer varies by what was asked.
        let here = get(path, &[("Accept-Encoding", "gzip")]);
        assert_eq!(here.status, 200, "{path}");
        assert_eq!(here.header("content-encoding"), "", "{path}");
        assert_eq!(here.header("etag"), tag, "{path}");
        let varies = (here.headers.iter())
            .any(|(name, value)| name == "vary" && value.contains("Accept-Encoding"));
        assert!(varies, "{path}");
        assert_eq!(here.body, first.body, "{path}");

        // Gzipped for a client that takes only gzip: the same tag, and the body decodes to
        // the plain one.
        let packed = get(path, &[("Accept-Encoding", "gzip, identity;q=0")]);
        assert_eq!(packed.status, 200, "{path}");
        assert_eq!(packed.header("content-encoding"), "gzip", "{path}");
        assert_eq!(packed.header("etag"), tag, "{path}");
        let mut decoded = Vec::new();
        GzDecoder::new(&packed.body[..])
            .read_to_end(&mut decoded)
            .unwrap();
        assert_eq!(decoded, first.body, "{path}");

        // A request a proxy on this machine handed on comes on loopback as well, and is packed
        // as before: its plain answer's tag, and a body that decodes to that plain answer.
        for marker in [
            ("X-Forwarded-For", "100.101.102.103"),
            ("Tailscale-User-Login", "someone@example.com"),
        ] {
            let plain = from_phone(path, &[marker]);
            assert_eq!(plain.status, 200, "{path} {marker:?}: {}", plain.text());
            let packed = from_phone(path, &[("Accept-Encoding", "gzip"), marker]);
            assert_eq!(packed.status, 200, "{path} {marker:?}");
            assert_eq!(
                packed.header("content-encoding"),
                "gzip",
                "{path} {marker:?}"
            );
            assert_eq!(
                packed.header("etag"),
                plain.header("etag"),
                "{path} {marker:?}"
            );
            let mut decoded = Vec::new();
            GzDecoder::new(&packed.body[..])
                .read_to_end(&mut decoded)
                .unwrap();
            assert_eq!(decoded, plain.body, "{path} {marker:?}");
        }
        let held = get(
            path,
            &[
                ("Accept-Encoding", "gzip, identity;q=0"),
                ("If-None-Match", &tag),
            ],
        );
        assert_eq!(held.status, 304, "{path}");

        // An agent's turn and another Host: tags of their own, and the owner's is no match.
        for caller in [
            ("X-Clarp-Turn", turn.as_str()),
            ("X-Clarp-Peer", "elsewhere"),
        ] {
            let theirs = get(path, &[caller]);
            assert_eq!(theirs.status, 200, "{path} {caller:?}: {}", theirs.text());
            assert!(
                theirs.header("etag").starts_with("W/\""),
                "{path} {caller:?}"
            );
            assert_ne!(theirs.header("etag"), tag, "{path} {caller:?}");
            let crossed = get(path, &[caller, ("If-None-Match", &tag)]);
            assert_eq!(crossed.status, 200, "{path} {caller:?}");
            let own = get(path, &[caller, ("If-None-Match", theirs.header("etag"))]);
            assert_eq!(own.status, 304, "{path} {caller:?}");
        }
    }
    // Every other route is kept nowhere and carries no tag.
    let other = get("/server-info", &[]);
    assert_eq!(other.header("cache-control"), "no-store");
    assert_eq!(other.header("etag"), "");
}
