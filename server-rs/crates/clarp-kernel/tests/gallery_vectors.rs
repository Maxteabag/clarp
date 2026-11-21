#![allow(clippy::unwrap_used)]

use clarp_kernel::gallery::{Image, Index, Refusal};
use clarp_kernel::media::Asset;
use serde_json::{Value, json};

fn public(row: &Image) -> Value {
    let row = row.row(Vec::new());
    json!({
        "asset_id": row["asset_id"],
        "favorite": row["favorite"],
        "favorite_at": row["favorite_at"],
        "used_as": row["used_as"],
    })
}

fn changed(index: &mut Index, ask: &Value, at: i64, op: &str) -> Result<Value, Refusal> {
    let id = ask["id"].as_str().unwrap();
    let previous = index.get(id).ok_or(Refusal::Unknown)?;
    let update = if op == "favorite" {
        previous.favorite(ask["value"].as_bool().unwrap(), at)?
    } else {
        previous.delete(ask["by"].as_str().unwrap(), at)?
    };
    let row = update.as_ref().unwrap_or(previous);
    let changed = update.is_some();
    let answer = if op == "favorite" {
        json!({"row": public(row), "changed": changed})
    } else {
        let removed = row.deletion.as_ref().unwrap();
        json!({"at": removed.at, "by": removed.by, "changed": changed})
    };
    if let Some(update) = update {
        index.apply(update);
    }
    Ok(answer)
}

#[test]
fn gallery_mutations_and_pages_follow_the_reference() {
    let mut index = Index::default();
    let vectors: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/gallery-vectors.json")).unwrap();
    let mut checked = 0;
    for step in vectors {
        let op = step["op"].as_str().unwrap();
        let ask = &step["ask"];
        let at = step["at"].as_i64().unwrap();
        let text = |key: &str| ask[key].as_str().unwrap();
        let limit = || usize::try_from(ask["limit"].as_u64().unwrap()).unwrap();
        let answer = match op {
            "seed" => {
                let image = Image::new(
                    Asset {
                        asset_id: text("id").to_owned(),
                        session: text("session").to_owned(),
                        mime_type: text("mime").to_owned(),
                        source_name: "image.png".to_owned(),
                        sha256: "digest".to_owned(),
                        extension: ".png".to_owned(),
                        bytes: 29,
                        width: Some(1),
                        height: Some(1),
                        caption: String::new(),
                        created_by: "agent".to_owned(),
                        created_at: at,
                        named_only: false,
                    },
                    text("session").to_owned(),
                );
                let answer = public(&image);
                index.apply(image);
                Ok(answer)
            }
            "favorite" | "delete" => changed(&mut index, ask, at, op),
            "page" => index
                .page(text("session"), ask["favorites"].as_bool().unwrap(), text("before"), limit())
                .map(|page| {
                    json!({
                        "assets": page.rows.into_iter().map(public).collect::<Vec<_>>(),
                        "next_before": page.next_before,
                        "total": page.total,
                        "favorites_total": page.favorites_total,
                    })
                }),
            "deleted" => Ok(json!(index.deleted(
                text("session"),
                ask["since"].as_i64().unwrap(),
                limit(),
                at
            ))),
            _ => panic!("unknown gallery operation {op}"),
        }
        .unwrap_or_else(|why| match why {
            Refusal::Removed(at) => json!({"error": why.words(), "deleted_at": at}),
            _ => json!({"error": why.words()}),
        });
        assert_eq!(answer, step["answer"], "{step}");
        checked += 1;
    }
    assert_eq!(checked, 45);
}
