#![allow(clippy::unwrap_used)]
use clarp_kernel::launch_directories as rules;
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};

#[test]
fn directory_answers_match_the_reference() {
    let suite: Value =
        serde_json::from_str(include_str!("fixtures/launch-directories.json")).unwrap();
    let home = suite["home"].as_str().unwrap();
    let mut ledger = rules::Ledger::new();
    assert!(rules::launched(&ledger, " \t", 0).is_none());
    for event in suite["launches"].as_array().unwrap() {
        let u = rules::launched(
            &ledger,
            &format!(" {} ", event[0].as_str().unwrap()),
            event[1].as_i64().unwrap(),
        )
        .unwrap();
        ledger.insert(u.path.clone(), u);
    }
    let owners = serde_json::from_value(suite["owners"].clone()).unwrap();
    let recent = rules::recent(&ledger, &owners);
    assert_eq!(json!(recent), suite["recent"]);
    let existing: BTreeSet<String> = serde_json::from_value(suite["existing"].clone()).unwrap();
    for c in suite["ranked"].as_array().unwrap() {
        let fallback = c["fallback"].as_bool().unwrap();
        let raw = (0..c["size"].as_u64().unwrap())
            .map(|i| {
                let suffix = if fallback && (i % 11 == 9 || i >= 998) {
                    "-Az"
                } else {
                    ""
                };
                format!("/synthetic/{i}{suffix}")
            })
            .collect::<Vec<_>>()
            .join("\n");
        let raw = c["raw"].as_array().map_or(raw, |v| {
            v.iter()
                .map(|p| p.as_str().unwrap())
                .collect::<Vec<_>>()
                .join("\n")
        });
        let seen = rules::ranked(&raw, c["q"].as_str().unwrap(), fallback);
        assert_eq!(json!(seen), c["seen"]);
    }
    for c in suite["completions"].as_array().unwrap() {
        let children = serde_json::from_value(c["names"].clone()).unwrap();
        assert_eq!(
            json!(rules::completions(children, c["prefix"].as_str().unwrap())),
            c["seen"]
        );
    }
    assert_eq!(suite["ranked"].as_array().unwrap().len(), 17);
    assert_eq!(suite["completions"].as_array().unwrap().len(), 18);
    for case in suite["spellings"].as_array().unwrap() {
        assert_eq!(
            rules::spelling(case["value"].as_str().unwrap()),
            case["expected"]
        );
    }
    let mut checked = 0;
    for f in suite["favorites"].as_array().unwrap() {
        assert_eq!(
            rules::favorites(&ledger, f["limit"].as_str().unwrap()),
            f["answer"]
        );
        checked += 1;
    }
    for c in suite["cases"].as_array().unwrap() {
        let q = rules::query(c["q"].as_str().unwrap());
        let mut explicit = Vec::new();
        if !q.is_empty() {
            let target = rules::explicit_path(q, home);
            if existing.contains(&target) {
                explicit.push(target.clone());
            }
            let path = Path::new(&target);
            let (parent, prefix) = if q.ends_with('/') {
                (path, "")
            } else {
                (
                    path.parent().unwrap_or(path),
                    path.file_name().and_then(|a| a.to_str()).unwrap_or(""),
                )
            };
            let children = existing
                .iter()
                .filter_map(|s| {
                    let p = Path::new(s);
                    (p.parent() == Some(parent))
                        .then(|| p.file_name().unwrap().to_str().unwrap().to_owned())
                })
                .collect();
            explicit.extend(
                rules::completions(children, prefix)
                    .into_iter()
                    .map(|s| parent.join(s).display().to_string()),
            );
        }
        let rows: Vec<String> = serde_json::from_value(c["rows"].clone()).unwrap();
        let backup: Vec<String> = serde_json::from_value(c["backup"].clone()).unwrap();
        let ranked = if c["fail"] == 1 {
            vec![]
        } else if c["rc"] == 0 {
            rules::ranked(&rows.join("\n"), q, false)
        } else if !q.is_empty() && c["fail"] != 2 {
            rules::ranked(&backup.join("\n"), q, true)
        } else {
            vec![]
        };
        let got = rules::lookup(
            home,
            q,
            recent.clone(),
            ranked,
            explicit,
            |s| {
                let expanded = s
                    .strip_prefix("~/")
                    .map_or_else(|| s.to_string(), |tail| format!("{home}/{tail}"));
                Some(rules::spelling(&expanded))
            },
            |s| existing.contains(s),
        );
        assert_eq!(got, c["answer"], "query {q:?}");
        checked += 1;
    }
    assert_eq!(checked, 48);
}
