//! PR 517 recorded from the reference's engine rules and both attempt loops.

#![allow(clippy::unwrap_used)]

use clarp_kernel::{
    call_transcription::{self as call, Attempts, Policy, Step},
    heard::{self, Failure, engine_order as order},
    languages,
};
use serde_json::{Value, json};

fn words(value: &Value) -> impl Iterator<Item = &str> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|word| word.as_str().unwrap())
}

fn mode<'a>(case: &'a Value, engine: &str) -> &'a str {
    case["outcomes"][engine].as_str().unwrap_or("ok")
}

fn handed(mode: &str) -> Option<&'static str> {
    match mode {
        "before-key" | "before-language" | "before-model" => None,
        "after-auto" => Some("auto"),
        _ => Some("en"),
    }
}

fn waited(engine: &str, has_fallback: bool) -> Value {
    json!({ "engine": engine, "wait": has_fallback.then_some(10.0) })
}

fn chat(case: &Value) -> Value {
    let names: Vec<_> = words(&case["order"]).map(str::to_owned).collect();
    let focus = case["focus"].as_str().unwrap();
    let plan = heard::plan("", &names, "", "", false);
    let (kept, skipped) = plan.chat_order(case["own"].as_str());
    let mut logs: Vec<_> = skipped
        .iter()
        .map(|engine| order::listed_before(focus, engine))
        .collect();
    let mut order = order::ChatOrder::new(kept);
    let mut tried = Vec::new();
    let mut failed = Vec::new();
    while let Some((engine, has_fallback)) = order.attempt() {
        tried.push(waited(engine, has_fallback));
        let mode = mode(case, engine);
        if mode == "ok" {
            return json!({"tried":tried,"logs":logs,"failed":[],"result":{"text":engine}});
        }
        let failure = order.failed(engine, handed(mode).is_some());
        logs.extend(
            failure
                .skipped
                .iter()
                .map(|(name, provider)| order::provider_already_had(focus, name, provider)),
        );
        if !failure.has_fallback {
            let recovered = mode == "before-language";
            let result = match mode {
                "before-language" => json!({"text":"recovered"}),
                "before-model" => json!({"status":400}),
                _ => json!({"status":500}),
            };
            let failed = if recovered { failed } else { Vec::new() };
            return json!({"tried":tried,"logs":logs,"failed":failed,"result":result});
        }
        if failure.reached {
            failed.push(engine);
        }
    }
    panic!("the fixture contains an attempt")
}

fn call(case: &Value) -> Value {
    let steps: Vec<_> = words(&case["order"])
        .enumerate()
        .map(|(index, engine)| Step {
            engine: engine.to_owned(),
            skip: case["pre_skipped"]
                .as_array()
                .unwrap()
                .contains(&json!(index))
                .then_some("language_incompatible"),
            adapted: Some(false),
            language: Some(Some("en".into())),
            local_default: false,
        })
        .collect();
    let policy = Policy::read("", "", "", "");
    let resolved = policy.resolve(None, &[], "en", "");
    let mut attempts = Attempts::new(&resolved);
    let mut contacted = order::Contacts::default();
    let mut tried = Vec::new();
    let mut result = json!({"error":"no transcription engine ran"});
    for (index, step) in steps.iter().enumerate() {
        if let Some(reason) = step.skip_reason(&contacted) {
            attempts.skipped(&step.engine, reason);
            continue;
        }
        let later = &steps[index + 1..];
        tried.push(waited(
            &step.engine,
            call::has_runnable_step(later, &contacted),
        ));
        let mode = mode(case, &step.engine);
        let boundary = handed(mode);
        if mode == "ok" {
            attempts.heard(&step.engine, boundary, 0, false);
            result = json!({"text":step.engine});
            break;
        }
        let internal_reason = match mode {
            "before-key" => "stand-in missing key",
            "before-model" => "stand-in unknown model",
            "before-language" => "stand-in unsupported set",
            _ => "stand-in provider error",
        };
        let failure = match mode {
            "before-model" => Failure::UnknownModel,
            _ => Failure::Failed,
        };
        attempts.failed(&step.engine, boundary, 0, failure.client_words().1);
        contacted.failed(&step.engine, boundary.is_some());
        if !call::has_runnable_step(later, &contacted) {
            result = json!({"error":internal_reason});
            break;
        }
    }
    json!({"tried":tried,"result":result,"attempts":attempts.finish()["attempts"]})
}

#[test]
fn provider_once_matches_the_recorded_python_paths() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/provider-once-vectors.json")).unwrap();
    let cases = |name: &str| vectors[name].as_array().unwrap();
    for case in cases("splits") {
        let parsed = languages::engines(case["input"].as_str().unwrap_or(""));
        assert_eq!(json!(parsed), case["engines"], "{case}");
    }
    let kept = vectors["stored"].to_string();
    for case in cases("merges") {
        let merged = languages::engines_changed(Some(&kept), &case["update"], &|model| {
            heard::cloud_model(model).is_some()
        });
        match merged {
            Ok(text) => assert_eq!(text, case["kept"], "{case}"),
            Err(error) => assert_eq!(error, case["error"], "{case}"),
        }
    }
    for case in cases("identity") {
        let engine = case["engine"].as_str().unwrap();
        assert_eq!(
            order::identity(engine, case["own"].as_str()),
            case["identity"],
            "{case}"
        );
    }
    for case in cases("distinct") {
        let (kept, skipped) = order::distinct_engines(words(&case["order"]), case["own"].as_str());
        assert_eq!(json!(kept), case["kept"], "{case}");
        assert_eq!(json!(skipped), case["skipped"], "{case}");
    }
    for case in cases("contacts") {
        let engine = case["engine"].as_str().unwrap();
        assert_eq!(
            order::hosted_provider(engine).unwrap_or(""),
            case["provider"],
            "{case}"
        );
        let mut contacts = order::Contacts::default();
        assert_eq!(
            json!(contacts.failed(engine, case["observed"] == true)),
            case["reached"],
            "{case}"
        );
        let kept: Vec<_> = words(&case["pending"])
            .filter(|later| contacts.already_sent(later).is_none())
            .collect();
        assert_eq!(json!(kept), case["kept"], "{case}");
    }
    for case in cases("call_policies") {
        let raw = case["raw"].as_str().unwrap();
        let policy = Policy::read("", raw, "local", "faster-whisper:small");
        assert_eq!(json!(policy.engines), case["snapshot"], "{case}");
        assert_eq!(json!(languages::engines(raw)), case["engines"], "{case}");
        let (_, saved) = policy
            .change(Some(&json!("")), Some(&case["raw"]), "local")
            .unwrap();
        assert_eq!(json!(saved), case["saved"], "{case}");
    }
    for case in cases("chat") {
        let replayed = chat(case);
        for key in ["tried", "logs", "failed", "result"] {
            assert_eq!(replayed[key], case[key], "{key}: {case}");
        }
    }
    for case in cases("calls") {
        let replayed = call(case);
        for key in ["tried", "attempts", "result"] {
            assert_eq!(replayed[key], case[key], "{key}: {case}");
        }
    }
    assert_eq!(
        [
            "splits",
            "merges",
            "identity",
            "distinct",
            "contacts",
            "call_policies",
            "chat",
            "calls"
        ]
        .map(|group| cases(group).len()),
        [11, 13, 72, 22, 72, 4, 13, 11]
    );
}
