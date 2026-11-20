//! A harness's model providers, held to what the Python Host's own module and route handlers
//! answered (`tools/harness-provider-vectors.py`).
#![allow(clippy::unwrap_used)]
use std::sync::LazyLock;

use clarp_kernel::harness_providers::{self as hp, Refused};
use serde_json::{Map, Value, json};

static CASES: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("fixtures/harness-provider-vectors.json")).unwrap()
});

fn refused(why: Refused) -> Value {
    match why {
        Refused::Bad(words) => json!({ "bad": words }),
        Refused::Conflict(words) => json!({ "conflict": words }),
    }
}

fn answered(why: Refused) -> Value {
    json!([why.status(), { "error": why.words() }])
}

#[test]
fn harnesses_and_providers_are_named_as_the_reference_names_them() {
    let mut raised = 0;
    for row in CASES["harnesses"].as_array().unwrap() {
        let got = hp::harness_id(&row[0]);
        if row[1].get("raises").is_some() {
            // The reference ends the connection; this Host refuses in the same words it uses
            // for any harness without providers.
            assert!(
                matches!(&got, Err(Refused::Bad(w)) if w.ends_with(" has no model providers")),
                "{row}"
            );
            raised += 1;
            continue;
        }
        let got = got.map_or_else(refused, |id| json!({ "ok": id }));
        assert_eq!(got, row[1], "{row}");
    }
    for row in CASES["providers"].as_array().unwrap() {
        let got = hp::provider_id(&row[0]).map_or_else(refused, |id| json!({ "ok": id }));
        assert_eq!(got, row[1], "{row}");
    }
    for row in CASES["secrets"].as_array().unwrap() {
        let limit = usize::try_from(row[1].as_u64().unwrap()).unwrap();
        let got = hp::clean_secret(&row[0], limit, row[2].as_str().unwrap())
            .map_or_else(refused, |kept| json!({ "ok": kept }));
        assert_eq!(got, row[3], "{row}");
    }
    let constants = &CASES["constants"];
    assert_eq!(constants["store_harness"], hp::STORE_HARNESS);
    assert_eq!(constants["timeout"], hp::LOGIN_TIMEOUT_SECONDS);
    assert_eq!(constants["ttl"], hp::METHODS_TTL_SECONDS);
    assert_eq!(constants["max_key"], hp::MAX_KEY_LENGTH);
    assert_eq!(constants["max_code"], hp::MAX_CODE_LENGTH);
    assert_eq!(constants["methods_key"], hp::METHODS_KEY);
    assert_eq!(constants["popular"], json!(hp::POPULAR));
    assert_eq!(
        [&CASES["harnesses"], &CASES["providers"], &CASES["secrets"]]
            .map(|r| r.as_array().unwrap().len()),
        [33, 33, 21]
    );
    assert_eq!(raised, 4);
}

#[test]
fn a_sign_in_is_told_with_its_code_and_its_errors_without_secrets() {
    for row in CASES["user_codes"].as_array().unwrap() {
        assert_eq!(
            hp::user_code(clarp_kernel::python::first(row[0].as_str().unwrap(), 500)),
            row[1],
            "{row}"
        );
    }
    let mut unworded = 0;
    for row in CASES["bounded"].as_array().unwrap() {
        let text = row[0].as_str().unwrap();
        if text.is_empty() {
            // The reference names an error without words by its class; this Host's errors
            // always have words.
            unworded += 1;
            continue;
        }
        assert_eq!(hp::bounded_error(text), row[1], "{row}");
    }
    for row in CASES["tasks"].as_array().unwrap() {
        let task = hp::task(&hp::Task {
            provider: row[0].as_str().unwrap(),
            status: row[1].as_str().unwrap(),
            started_at: row[2].as_i64().unwrap(),
            method_label: row[3].as_str().unwrap(),
            authorization: &row[4],
            error: row[5].as_str().unwrap(),
            code_submitted: row[6].as_bool().unwrap(),
        });
        assert_eq!(task, row[7], "{row}");
        let keys: Vec<_> = task.as_object().unwrap().keys().collect();
        let want: Vec<_> = row[7].as_object().unwrap().keys().collect();
        assert_eq!(keys, want, "the order of a task's fields");
    }
    assert_eq!(hp::idle("openai"), CASES["tasks"][0][7]);
    assert_eq!(
        ["user_codes", "bounded", "tasks"].map(|k| CASES[k].as_array().unwrap().len()),
        [22, 23, 12]
    );
    assert_eq!(unworded, 1);
}

#[test]
fn sign_in_methods_are_kept_as_the_reference_keeps_them() {
    let mut failed = 0;
    for row in CASES["methods"].as_array().unwrap() {
        let got = hp::usable_methods(&row[0]);
        if row[1].get("raises").is_some() {
            assert_eq!(got, None, "{row}");
            failed += 1;
        } else {
            assert_eq!(Value::Object(got.unwrap()), row[1]["ok"], "{row}");
        }
    }
    let mut unread = 0;
    for row in CASES["remembered"].as_array().unwrap() {
        let got = hp::remembered(row[0].as_str());
        if row[1].get("raises").is_some() {
            // The reference fails the listing on a moment it cannot read; this Host takes
            // it as never, and asks OpenCode again.
            assert_eq!(got.at, 0.0, "{row}");
            unread += 1;
            continue;
        }
        let want = &row[1]["ok"];
        assert_eq!(Value::Object(got.methods.clone()), want[0], "{row}");
        assert_eq!(got.version, want[1], "{row}");
        assert_eq!(got.at, want[2].as_f64().unwrap(), "{row}");
        let again = hp::remembered(Some(&hp::remembered_text(
            &got.version,
            got.at,
            &got.methods,
        )));
        if got.at != 0.0 && !got.methods.is_empty() {
            assert_eq!(again, got, "what is kept is read back");
        }
    }
    assert!(!hp::fresh(0.0, 10.0));
    assert!(hp::fresh(10.0, 10.0 + hp::METHODS_TTL_SECONDS - 0.5));
    assert!(!hp::fresh(10.0, 10.0 + hp::METHODS_TTL_SECONDS));
    assert_eq!(
        ["methods", "remembered"].map(|k| CASES[k].as_array().unwrap().len()),
        [11, 16]
    );
    assert_eq!((failed, unread), (2, 1));
}

/// What a recorded case read: OpenCode's two stores, the remembered methods, the tasks.
struct Setup {
    catalogue: Map<String, Value>,
    auth: Result<Option<Vec<u8>>, ()>,
    methods: Map<String, Value>,
    installed: bool,
    tasks: Value,
    locked: bool,
}

fn text(field: &Value) -> Option<&'static str> {
    let name = field["text"].as_str()?;
    CASES["texts"][name].as_str()
}

fn setup(case: &Value) -> Setup {
    let catalogue = text(&case["catalogue"]);
    let auth = match &case["auth"] {
        Value::Null => Ok(None),
        field if field["directory"] == true => Err(()),
        field => Ok(Some(text(field).unwrap().as_bytes().to_vec())),
    };
    Setup {
        catalogue: hp::catalogue(catalogue.map(str::as_bytes)),
        auth,
        methods: hp::remembered(text(&case["remembered"])).methods,
        installed: case["installed"].as_bool().unwrap_or(true),
        tasks: case
            .get("tasks")
            .cloned()
            .filter(|t| !t.is_null())
            .unwrap_or(json!({})),
        locked: case["locked"] == true,
    }
}

fn env(name: &str) -> bool {
    CASES["env_set"]
        .as_array()
        .unwrap()
        .iter()
        .any(|set| set == name)
}

/// What this Host answers a recorded request, and the store it then writes.
fn answer(case: &Value) -> (Value, Option<String>) {
    let s = setup(case);
    let auth = s.auth.as_ref().map(|read| read.as_deref()).map_err(|_| ());
    let query = |name: &str| json!(case["query"][name].as_str().unwrap_or(""));
    let body = &case["body"];
    let field = |name: &str| body.get(name).cloned().unwrap_or(Value::Null);
    let types = |store: &Map<String, Value>| hp::credential_types(store);
    let written = |provider: &str, store: Map<String, Value>| {
        if s.locked {
            return (json!([500, { "error": hp::UNWRITABLE }]), None);
        }
        let row = hp::provider_row(provider, &s.catalogue, &types(&store), &s.methods, &env);
        (
            json!([200, { "ok": true, "provider": row }]),
            Some(hp::store_text(&store)),
        )
    };
    let bad_json = || (json!([400, { "error": "bad json" }]), None);
    match case["route"].as_str().unwrap() {
        "_handle_harness_providers" => match hp::harness_id(&query("harness")) {
            Err(why) => (answered(why), None),
            Ok(harness) => {
                let read = hp::Read {
                    catalogue: &s.catalogue,
                    types: &hp::listed_types(auth),
                    methods: &s.methods,
                    env: &env,
                };
                (
                    json!([
                        200,
                        hp::listing(harness, &read, s.tasks.clone(), s.installed)
                    ]),
                    None,
                )
            }
        },
        "_handle_harness_provider_models" => (
            hp::models(&query("harness"), &query("provider"), &s.catalogue)
                .map_or_else(answered, |listed| json!([200, listed])),
            None,
        ),
        "_handle_harness_provider_login" if body.is_null() => bad_json(),
        "_handle_harness_provider_login" if body.get("key").is_some() => {
            match hp::save_key(&field("harness"), &field("provider"), &field("key"), auth) {
                Err(why) => (answered(why), None),
                Ok((provider, store)) => written(&provider, store),
            }
        }
        "_handle_harness_provider_login" => {
            let asked = hp::start(
                &field("harness"),
                &field("provider"),
                &field("method"),
                &field("inputs"),
                &s.methods,
                &s.catalogue,
                s.installed,
            );
            match asked {
                Err(why) => (answered(why), None),
                Ok(start) => {
                    let current = &s.tasks[&start.provider];
                    if current["status"] == "running" {
                        return (json!([202, current]), None);
                    }
                    let failed = hp::task(&hp::Task {
                        provider: &start.provider,
                        status: "failed",
                        started_at: CASES["now_ms"].as_i64().unwrap(),
                        method_label: &start.label,
                        authorization: &Value::Null,
                        error: &hp::bounded_error(hp::NOT_STARTED),
                        code_submitted: false,
                    });
                    (json!([202, failed]), None)
                }
            }
        }
        "_handle_harness_provider_login_code" if body.is_null() => bad_json(),
        "_handle_harness_provider_login_code" => {
            match hp::code(&field("harness"), &field("provider"), &field("code")) {
                Err(why) => (answered(why), None),
                // No sign-in of the recording's ever waits for a code.
                Ok(_) => (
                    answered(Refused::Conflict(hp::NO_CODE_WAITING.into())),
                    None,
                ),
            }
        }
        "_handle_harness_provider_logout" if body.is_null() => bad_json(),
        "_handle_harness_provider_logout" => {
            let out = hp::logout(
                &field("harness"),
                &field("provider"),
                auth,
                &s.catalogue,
                &s.methods,
                &env,
            );
            match out {
                Err(why) => (answered(why), None),
                Ok((provider, store)) => written(&provider, store),
            }
        }
        other => panic!("no route {other}"),
    }
}

#[test]
fn every_route_answers_and_writes_as_the_reference() {
    let mut counts = std::collections::BTreeMap::<String, usize>::new();
    for case in CASES["routes"].as_array().unwrap() {
        let (got, store) = answer(case);
        let route = case["route"].as_str().unwrap().to_string();
        let what = if case["answer"].get("raises").is_some() {
            // The reference ends the connection on these; this Host answers: a harness that
            // is not a text is refused, a catalogue entry of another shape lists less.
            let status = got[0].as_u64().unwrap();
            assert!(status == 400 || status == 200, "{case}: {got}");
            "raised"
        } else {
            assert_eq!(got, case["answer"], "{case}");
            if case["answer"][0] == 200 && got[1]["providers"].is_array() {
                let order = |v: &Value| -> Vec<Value> {
                    v["providers"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|r| r["id"].clone())
                        .collect()
                };
                assert_eq!(
                    order(&got[1]),
                    order(&case["answer"][1]),
                    "the order of {case}"
                );
            }
            if let Some(store) = store {
                assert_eq!(store, case["store"]["text"].as_str().unwrap(), "{case}");
                assert_eq!(case["store"]["mode"], 0o600, "{case}");
                "written"
            } else {
                "answered"
            }
        };
        *counts.entry(format!("{route} {what}")).or_default() += 1;
    }
    let counted: usize = counts.values().sum();
    assert_eq!(counted, 257, "{counts:?}");
    assert_eq!(
        counts.get("_handle_harness_provider_login written"),
        Some(&15),
        "{counts:?}"
    );
    assert_eq!(
        counts.get("_handle_harness_provider_logout written"),
        Some(&4),
        "{counts:?}"
    );
    let raised: usize = counts
        .iter()
        .filter(|(k, _)| k.ends_with("raised"))
        .map(|(_, n)| n)
        .sum();
    assert_eq!(raised, 3, "{counts:?}");
}

#[test]
fn names_that_differ_only_in_case_or_by_their_letters_sort_as_python_sorts_them() {
    // The listing of every provider with no harness asked: Python's `str.lower()` against
    // Rust's `to_lowercase()`, for names that differ only in case and for letters beyond
    // ASCII (the lead's ruling of 20 Nov 2026).
    let case = CASES["routes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["route"] == "_handle_harness_providers" && c["query"]["harness"] == "opencode")
        .unwrap();
    let names: Vec<&str> = case["answer"][1]["providers"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["signed_in"] == false && r["popular"] == false)
        .map(|r| r["name"].as_str().unwrap())
        .collect();
    for pair in [
        ["alpha", "Alpha"],
        ["apfel", "\u{c4}pfel"],
        ["SS-labs", "\u{df}-labs"],
    ] {
        let at = |name: &str| names.iter().position(|n| *n == name).unwrap();
        assert!(at(pair[0]) < at(pair[1]), "{names:?}");
    }
    assert!(names.contains(&"\u{3a3}\u{391}\u{3a3}") && names.contains(&"\u{130}nci"));
    let (got, _) = answer(case);
    assert_eq!(got, case["answer"]);
}

#[test]
fn an_answer_to_the_authorize_call_is_taken_as_the_reference_takes_it() {
    let now = CASES["now_ms"].as_i64().unwrap();
    let mut failures = 0;
    for row in CASES["authorize"].as_array().unwrap() {
        let (answer, out) = (&row[0], &row[1]);
        let taken = hp::authorization(answer);
        let task = |status: &str, authorization: &Value, error: &str| {
            hp::task(&hp::Task {
                provider: "openai",
                status,
                started_at: now,
                method_label: "",
                authorization,
                error,
                code_submitted: false,
            })
        };
        if out.get("raises").is_some() {
            // R156, the reference's failure: it raises and leaves the sign-in running with
            // its server open. This Host fails the sign-in and ends the server.
            assert_eq!(taken, None, "{row}");
            assert_eq!(
                (&out["task"]["status"], &out["server_ended"]),
                (&json!("running"), &json!(false))
            );
            failures += 1;
        } else if out["answer"]["status"] == "failed" {
            // The reference repaired (R156): the same answer.
            assert_eq!(taken, None, "{row}");
            assert_eq!(
                out["answer"],
                task("failed", &Value::Null, hp::NOT_ANSWERED),
                "{row}"
            );
        } else {
            let authorization = taken.unwrap_or_else(|| panic!("{row}"));
            assert_eq!(out["answer"], task("running", &authorization, ""), "{row}");
            assert_eq!(out["task"], out["answer"], "{row}");
        }
    }
    assert_eq!(CASES["authorize"].as_array().unwrap().len(), 13);
    assert_eq!(failures, 4);
}
