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
    let host = place.start();
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

        // Gzipped: the same tag, and the body decodes to the plain one.
        let packed = get(path, &[("Accept-Encoding", "gzip")]);
        assert_eq!(packed.status, 200, "{path}");
        assert_eq!(packed.header("content-encoding"), "gzip", "{path}");
        assert_eq!(packed.header("etag"), tag, "{path}");
        let mut decoded = Vec::new();
        GzDecoder::new(&packed.body[..])
            .read_to_end(&mut decoded)
            .unwrap();
        assert_eq!(decoded, first.body, "{path}");
        let held = get(
            path,
            &[("Accept-Encoding", "gzip"), ("If-None-Match", &tag)],
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
