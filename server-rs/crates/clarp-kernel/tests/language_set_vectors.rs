//! Python's R152 choices and wire shapes after R175.

#![allow(clippy::unwrap_used)]

use clarp_kernel::heard::{self, engine_order::ChatOrder, language_set as set};
use clarp_kernel::languages;
use serde_json::{Value, json};

fn words(value: &Value) -> Vec<&str> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|word| word.as_str().unwrap())
        .collect()
}

fn own<'a>(case: &Value, id: &'a str, declared: &'a [&'a str]) -> set::Own<'a> {
    if case["enabled"] == false {
        return set::Own::Off;
    }
    if case["declared"] == "unavailable" {
        return set::Own::Unavailable;
    }
    set::Own::Model {
        id,
        languages: declared,
    }
}

#[test]
fn the_language_set_recovery_answers_the_python_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/language-set-vectors.json")).unwrap();
    let selections = vectors["selections"].as_array().unwrap();
    for case in selections {
        let model = case["model"].as_str().unwrap();
        let selected = set::selected(model, |_| case["keyed"] == true).unwrap_or("");
        assert_eq!(selected, case["selected"], "{case}");
    }

    let declarations = vectors["own"].as_array().unwrap();
    for case in declarations {
        let id = format!("faster-whisper:{}", case["model"].as_str().unwrap());
        let declared = words(&case["declared"]);
        let actual = set::own_refusal(own(case, &id, &declared), &words(&case["languages"]));
        assert_eq!(actual.as_deref().unwrap_or(""), case["refusal"], "{case}");
    }

    let fallbacks = vectors["fallbacks"].as_array().unwrap();
    for case in fallbacks {
        let id = format!("faster-whisper:{}", case["model"].as_str().unwrap());
        let declared = words(&case["declared"]);
        let selected = set::selected(case["selected"].as_str().unwrap(), |_| {
            case["keyed"] == true
        });
        let from = case["from"].as_str().unwrap();
        let language = case["language"].as_str().unwrap();
        let codes: Vec<_> = language.split(',').collect();
        let attempted = words(&case["attempted"]);
        let tried: Vec<_> = attempted
            .iter()
            .map(|model| set::identity(model, Some(&id)))
            .collect();
        let decision = set::fallback(
            set::identity(from, Some(&id)),
            selected,
            &tried,
            own(case, &id, &declared),
            &codes,
        );
        let (calls, result) = match decision {
            set::Fallback::Hosted(to) => {
                let result = if case["hosted_error"] == true {
                    // A chosen hosted fallback's transport failure belongs to its usual path.
                    json!({"status": 500})
                } else {
                    let provider = heard::provider_of(to);
                    let handed = languages::handed(language, &provider).unwrap();
                    set::receipt(from, to, handed.as_deref())
                };
                (json!([to]), result)
            }
            set::Fallback::Own(mut reasons) => {
                let to = "server-default";
                let result = match case["own_error"].as_str().unwrap() {
                    "busy" => json!({"status": 429}),
                    "loading" => json!({"status": 503}),
                    "failed" => {
                        reasons.push(set::OWN_FAILED.into());
                        set::refusal(from, &codes, &reasons)
                    }
                    _ => set::receipt(from, to, None),
                };
                (json!([to]), result)
            }
            set::Fallback::Refused(reasons) => (json!([]), set::refusal(from, &codes, &reasons)),
        };
        assert_eq!(calls, case["calls"], "{}", case["case"]);
        assert_eq!(result, case["result"], "{}", case["case"]);
    }

    assert_eq!(
        (selections.len(), declarations.len(), fallbacks.len()),
        (14, 10, 20)
    );
}

#[test]
fn the_same_provider_or_local_default_is_attempted_only_once_for_a_set() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/language-set-vectors.json")).unwrap();
    let repeated = &vectors["reference_repeats_provider"];
    let ordinary: Vec<_> = words(&repeated["order"])
        .into_iter()
        .map(str::to_string)
        .collect();
    let plan = heard::plan("", &ordinary, "", "", false);
    let (kept, _) = plan.chat_order(None);
    assert_eq!(kept, ["elevenlabs:scribe_v2", "deepgram:nova-3"]);
    let mut pending = ChatOrder::new(kept);
    let mut sent = Vec::new();
    while let Some((engine, _)) = pending.attempt() {
        let reached = engine.starts_with("elevenlabs:");
        if reached {
            sent.push(engine);
        }
        pending.failed(engine, reached);
    }
    assert_eq!(sent, words(&repeated["sent"]));
    let aliases = [
        "local",
        "server-default",
        "faster-whisper:small",
        "cartesia:ink-2",
        "cartesia:ink-whisper",
        "faster-whisper:base",
    ]
    .map(str::to_string);
    let plan = heard::plan("", &aliases, "", "", false);
    assert_eq!(
        plan.chat_order(Some("faster-whisper:small")).0,
        [
            "local",
            "cartesia:ink-2",
            "cartesia:ink-whisper",
            "faster-whisper:base"
        ]
    );
}
