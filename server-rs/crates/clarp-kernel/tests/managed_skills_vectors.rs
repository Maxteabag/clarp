//! The skills kept installed for the tools, held to the Python Host's own functions
//! (`server-rs/tools/managed-skills-vectors.py`, run in stand-in homes and release trees).
#![allow(clippy::unwrap_used, clippy::expect_used)]
use std::collections::HashMap;

use clarp_kernel::managed_skills::{
    self as skills, Act, At, Facts, Link, Malformed, Refusal, Root,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/managed-skills-vectors.json")).unwrap()
}

fn root(name: &str) -> Root {
    match name {
        "claude" => Root::Claude,
        "codex" => Root::Codex,
        "agy" => Root::Agy,
        other => panic!("no folder {other}"),
    }
}

fn link(name: &str) -> Link {
    match name {
        "missing" => Link::Missing,
        "modified" | "dir" | "foreign" => Link::Modified,
        "healthy" => Link::Healthy,
        "outdated" => Link::Outdated,
        other => panic!("no state {other}"),
    }
}

fn texts(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|item| item.as_str().unwrap().to_owned())
                .collect()
        })
        .unwrap_or_default()
}

/// A lookup of programs that answers what the reference's `shutil.which` answered, and fails
/// the test when asked for a program the reference never looked up.
fn which(found: &Value) -> impl FnMut(&str) -> bool + '_ {
    move |name: &str| {
        found
            .get(name)
            .unwrap_or_else(|| panic!("{name} was never looked up by the reference"))
            .as_bool()
            .unwrap()
    }
}

#[test]
fn a_link_in_a_tools_folder_reads_as_the_reference_reads_it() {
    let cases = vectors();
    let mut counted = HashMap::new();
    for case in cases["link_states"].as_array().unwrap() {
        let resolved = case["resolved"].as_str().unwrap_or("");
        let at = match case["at"].as_str().unwrap() {
            "nothing" => At::Nothing,
            "thing" => At::Thing,
            _ => At::Link(resolved),
        };
        let releases = [case["releases"].as_str().unwrap()];
        let state = skills::link_state(
            at,
            case["source"].as_str().unwrap(),
            &releases,
            case["name"].as_str().unwrap(),
        );
        assert_eq!(state.as_str(), case["state"], "{case}");
        *counted.entry(state.as_str()).or_insert(0) += 1;
    }
    assert_eq!(counted.values().sum::<usize>(), 20);
    assert_eq!(counted["healthy"], 5);
    assert_eq!(counted["outdated"], 3);
}

#[test]
fn a_link_into_another_hosts_releases_is_outdated_where_that_tree_is_named() {
    // The Rust Host's difference (the lead's ruling of 20 Nov 2026): the Python Host's
    // releases are Clarp's own, so a link it left reads outdated, not foreign.
    let python = "/R/python-share/releases";
    let own = "/R/rust-share/releases";
    let source = "/R/rust-share/releases/r2/skills/s";
    let left = At::Link("/R/python-share/releases/r0/skills/s");
    assert_eq!(
        skills::link_state(left, source, &[own, python], "s"),
        Link::Outdated
    );
    assert_eq!(
        skills::link_state(left, source, &[own], "s"),
        Link::Modified
    );
    let other_name = At::Link("/R/python-share/releases/r0/skills/t");
    assert_eq!(
        skills::link_state(other_name, source, &[own, python], "s"),
        Link::Modified
    );
    let deeper = At::Link("/R/python-share/releases/r0/skills/s/x");
    assert_eq!(
        skills::link_state(deeper, source, &[own, python], "s"),
        Link::Modified
    );
    // A folder whose name begins with the releases folder's is not it.
    let beside = At::Link("/R/python-share/releases-x/skills/s");
    assert_eq!(
        skills::link_state(beside, source, &[own, python], "s"),
        Link::Modified
    );
}

#[test]
fn the_requirements_of_a_skill_are_met_as_the_reference_meets_them() {
    let cases = vectors();
    let (mut met, mut unmet, mut malformed) = (0, 0, 0);
    for case in cases["requirements"].as_array().unwrap() {
        let mut found = which(&case["found"]);
        let outcome = skills::requirements(&case["item"], &mut found);
        match &case["outcome"] {
            Value::String(_) => {
                assert_eq!(outcome, Err(Malformed), "{case}");
                malformed += 1;
            }
            expected => {
                let (ok, missing) = outcome.unwrap();
                assert_eq!(json!([ok, missing]), *expected, "{case}");
                if ok { met += 1 } else { unmet += 1 }
            }
        }
    }
    assert_eq!((met, unmet, malformed), (16, 21, 2));
}

#[test]
fn the_folders_written_are_the_distinct_usable_ones() {
    let cases = vectors();
    let mut usable = 0;
    for case in cases["distinct"].as_array().unwrap() {
        let facts = &case["facts"];
        let is = |name: &str| facts[name].as_bool().unwrap();
        let agy = skills::agy_usable(is("parent_is_dir"), is("is_link"), is("is_dir"));
        assert_eq!(agy, case["usable"], "{case}");
        usable += usize::from(agy);
        let offered: Vec<(Root, String)> = case["offered"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| {
                (
                    root(pair[0].as_str().unwrap()),
                    pair[1].as_str().unwrap().to_owned(),
                )
            })
            .collect();
        let kept: Vec<&str> = skills::distinct(&offered)
            .into_iter()
            .map(Root::name)
            .collect();
        assert_eq!(json!(kept), case["kept"], "{case}");
    }
    assert_eq!(usable, 6);
}

#[test]
fn a_manifest_is_read_or_refused_as_the_reference_reads_it() {
    let cases = vectors();
    let (mut read, mut unavailable, mut crashed) = (0, 0, 0);
    for case in cases["manifests"].as_array().unwrap() {
        let bytes = case["manifest"].as_str().map(str::as_bytes);
        let items = skills::manifest(bytes);
        let outcome = &case["outcome"];
        match outcome[0].as_str().unwrap() {
            "unavailable" => {
                assert_eq!(items, Err(Malformed), "{case}");
                assert_eq!(outcome[1], skills::UNAVAILABLE);
                unavailable += 1;
            }
            "rows" => {
                let items = items.unwrap();
                for item in &items {
                    skills::skill_id(item).unwrap();
                }
                let ids: Vec<&Value> = items.iter().map(|item| &item["id"]).collect();
                assert_eq!(json!(ids), outcome[1], "{case}");
                read += 1;
            }
            _ => {
                let items = items.unwrap();
                assert!(
                    items.iter().any(|item| skills::skill_id(item).is_err()),
                    "{case}"
                );
                crashed += 1;
            }
        }
    }
    assert_eq!((read, unavailable, crashed), (3, 11, 3));
}

#[test]
fn every_row_of_the_status_is_the_references_row() {
    let cases = vectors();
    let mut counted: HashMap<String, usize> = HashMap::new();
    for run in cases["status"].as_array().unwrap() {
        let chosen = texts(&run["chosen"]);
        let present = texts(&run["present"]);
        let items = run["manifest"].as_array().unwrap();
        let rows = run["rows"].as_array().unwrap();
        assert_eq!(items.len(), rows.len());
        for (item, expected) in items.iter().zip(rows) {
            let id = skills::skill_id(item).unwrap();
            let mut links = vec![
                (
                    Root::Claude,
                    link(expected["links"]["claude"].as_str().unwrap()),
                ),
                (
                    Root::Codex,
                    link(expected["links"]["codex"].as_str().unwrap()),
                ),
            ];
            assert_eq!(expected["links"].get("agy").is_some(), run["agy"] == true);
            if let Some(agy) = expected["links"]["agy"].as_str() {
                links.push((Root::Agy, link(agy)));
            }
            let mut found = which(&run["found"]);
            let facts = Facts {
                links,
                enabled: chosen.contains(&id),
                source: present.contains(&id),
                requirements: skills::requirements(item, &mut found).unwrap(),
            };
            let row = skills::row(item, &facts);
            assert_eq!(row, *expected, "{item}");
            // The fields keep the reference's order: the entry's own, then what was found.
            let keys: Vec<&String> = row.as_object().unwrap().keys().collect();
            let told: Vec<&String> = expected.as_object().unwrap().keys().collect();
            assert_eq!(keys, told);
            *counted
                .entry(expected["health"].as_str().unwrap().to_owned())
                .or_default() += 1;
        }
    }
    assert_eq!(counted.values().sum::<usize>(), 602);
    assert_eq!(counted.len(), 6, "{counted:?}");
}

#[test]
fn a_change_does_in_each_folder_what_the_reference_does() {
    let cases = vectors();
    let (mut done, mut refused) = (0, 0);
    for case in cases["changes"].as_array().unwrap() {
        let skill = case["skill"].as_str().unwrap();
        let enabled = case["enabled"].as_bool().unwrap();
        let items = case["manifest"].as_array().unwrap();
        let offered: Vec<(Root, String)> = case["roots"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| {
                (
                    root(pair[0].as_str().unwrap()),
                    pair[1].as_str().unwrap().to_owned(),
                )
            })
            .collect();
        let used = skills::distinct(&offered);
        let names: Vec<&str> = used.iter().map(|root| root.name()).collect();
        assert_eq!(json!(names), case["used"], "{case}");
        let before = |root: Root| case["before"][root.name()].as_str().unwrap();
        let roots: Vec<(Root, Link)> = used
            .iter()
            .map(|root| (*root, link(before(*root))))
            .collect();
        let item = skills::entry(items, skill).unwrap();
        let outcome = skills::change(item, skill, enabled, case["present"] == true, &roots);
        let chosen = texts(&case["chosen"]);
        match (case["result"][0].as_str().unwrap(), outcome) {
            ("refused", Err(refusal)) => {
                let place = |root: Root| {
                    case["destinations"][root.name()]
                        .as_str()
                        .unwrap()
                        .to_owned()
                };
                assert_eq!(refusal.words(&place), case["result"][1], "{case}");
                assert_eq!(case["after"], case["before"], "{case}");
                assert_eq!(case["chosen_after"], case["chosen"], "{case}");
                refused += 1;
            }
            ("ok", Ok(acts)) => {
                assert_eq!(
                    skills::changed(item.unwrap(), enabled),
                    case["result"][1],
                    "{case}"
                );
                // Each folder ends as the act on the folder it is (a shared one by its first).
                for (name, after) in case["after"].as_object().unwrap() {
                    let here = &offered
                        .iter()
                        .find(|(root, _)| root.name() == name)
                        .map(|(_, p)| p.clone());
                    let writer = match here {
                        Some(path) => offered.iter().find(|(_, p)| p == path).unwrap().0,
                        None => root(name),
                    };
                    let act = acts
                        .iter()
                        .find(|(root, _)| *root == writer)
                        .map(|(_, act)| *act);
                    let was = before(root(name));
                    let expected = match act {
                        Some(Act::Link) => "healthy",
                        Some(Act::Unlink) => "missing",
                        Some(Act::Leave) | None => was,
                    };
                    assert_eq!(after, expected, "{name} in {case}");
                }
                let after = skills::chosen_after(&chosen, skill, enabled);
                assert_eq!(json!(after), case["chosen_after"], "{case}");
                done += 1;
            }
            (_, outcome) => panic!("{outcome:?} for {case}"),
        }
    }
    assert_eq!((done, refused), (290, 354));
}

#[test]
fn a_change_is_asked_as_the_reference_reads_its_body() {
    let cases = vectors();
    let routes = &cases["routes"];
    let (mut taken, mut refused) = (0, 0);
    for case in routes["asked"].as_array().unwrap() {
        let body = (!case[0].is_null()).then_some(&case[0]);
        match skills::asked(body) {
            Ok((skill, enabled)) => {
                assert_eq!(case[1], json!([[skill, enabled]]), "{case}");
                assert_eq!(case[2][0], 200);
                taken += 1;
            }
            Err(words) => {
                assert_eq!(case[1], json!([]), "{case}");
                assert_eq!(case[2], json!([400, {"error": words}]), "{case}");
                refused += 1;
            }
        }
    }
    assert_eq!((taken, refused), (9, 16));
    // How the two routes answer a refusal and a failure of the disk.
    assert_eq!(routes["failures"][0][1][0], 409);
    assert_eq!(routes["failures"][1][1][0], 500);
    assert_eq!(
        routes["reads"][1][1],
        json!([503, {"error": skills::UNAVAILABLE}])
    );
    assert_eq!(routes["reads"][2][1][0], 503);
}

#[test]
fn a_start_links_the_chosen_skills_and_leaves_what_is_not_clarps() {
    let items = vec![
        json!({"id": "c1", "pack": "core"}),
        json!({"id": "a1", "pack": "artifacts"}),
        json!({"id": "a2", "pack": "artifacts", "default_enabled": false}),
        json!({"id": "a3", "pack": "artifacts", "default_enabled": 0}),
        json!({"id": "o1", "pack": "optional"}),
        json!({"id": "c0", "pack": "core"}),
    ];
    // With nothing to take over: the Python Host's setup's choice.
    assert_eq!(
        skills::first_chosen(None, &items).unwrap(),
        ["a1", "c0", "c1"]
    );
    let empty = json!({"channel": "stable"});
    assert_eq!(
        skills::first_chosen(Some(&empty), &items).unwrap(),
        ["a1", "c0", "c1"]
    );
    // The other Host's list, as it is: nothing added, doubles and non-texts dropped.
    let other = json!({"skills": ["o1", 5, "o1", "gone"]});
    assert_eq!(
        skills::first_chosen(Some(&other), &items).unwrap(),
        ["gone", "o1"]
    );
    let none = json!({"skills": []});
    assert!(
        skills::first_chosen(Some(&none), &items)
            .unwrap()
            .is_empty()
    );
    // A start enrols the core skills a newer release brought.
    let chosen = ["o1".to_owned(), "gone".to_owned()];
    assert_eq!(
        skills::enrolled(&chosen, &items).unwrap(),
        ["c0", "c1", "gone", "o1"]
    );
    assert_eq!(
        skills::enrolled(&chosen, &[json!({"pack": "core"})]),
        Err(Malformed)
    );
    for (link, in_release, act) in [
        (Link::Missing, true, Act::Link),
        (Link::Outdated, true, Act::Link),
        (Link::Healthy, true, Act::Leave),
        (Link::Modified, true, Act::Leave),
        (Link::Outdated, false, Act::Unlink),
        (Link::Missing, false, Act::Leave),
        (Link::Modified, false, Act::Leave),
    ] {
        assert_eq!(
            skills::at_start(link, in_release),
            act,
            "{link:?} {in_release}"
        );
    }
    assert_eq!(
        Refusal::Preserving(Root::Agy).words(&|root| format!("/x/{}", root.name())),
        "preserving non-Clarp skill at /x/agy"
    );
}
