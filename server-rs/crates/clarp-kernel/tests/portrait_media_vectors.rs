#![allow(clippy::unwrap_used)]

use clarp_kernel::gallery::{Deletion, Image, Index};
use clarp_kernel::media::Asset;
use serde_json::{Value, json};

#[test]
fn portrait_selection_matches_the_python_hosts_published_by() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/portrait-media-vectors.json")).unwrap();
    let mut index = Index::default();
    for row in vectors["rows"].as_array().unwrap() {
        let text = |name: &str| row[name].as_str().unwrap().to_owned();
        let mut image = Image::new(
            Asset {
                asset_id: text("id"),
                session: text("session"),
                mime_type: text("mime"),
                created_by: text("created_by"),
                created_at: row["created_at"].as_i64().unwrap(),
                source_name: "image".into(),
                sha256: "digest".into(),
                extension: ".png".into(),
                bytes: 1,
                width: None,
                height: None,
                caption: String::new(),
                named_only: false,
            },
            text("session"),
        );
        image.deletion = row["deleted_at"].as_i64().map(|at| Deletion {
            at,
            by: String::new(),
        });
        index.apply(image);
    }
    let ids = |items: Vec<&Image>| {
        json!(
            items
                .into_iter()
                .map(|i| i.asset.asset_id.clone())
                .collect::<Vec<_>>()
        )
    };
    let cases = vectors["queries"].as_array().unwrap();
    for row in cases {
        let (kept, discarded) = index.published_by(
            row["session"].as_str().unwrap(),
            row["producer"].as_str().unwrap(),
            row["since"].as_i64().unwrap(),
            row["until"].as_i64().unwrap(),
        );
        assert_eq!(ids(kept), row["kept"], "{row}");
        assert_eq!(ids(discarded), row["discarded"], "{row}");
    }
    assert_eq!(cases.len(), 13);
}
