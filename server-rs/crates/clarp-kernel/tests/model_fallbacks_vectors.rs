//! Global Janitor policy and bounded fallback behavior from the Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::janitors::{
    book::{Agent, Book, Config, World},
    fallbacks::{self, Pick},
};
use serde_json::{Value, json};
struct Host;
impl World for Host {
    fn agent(&self, key: &str) -> Option<Agent> {
        ["router", "keeper", "scheduled", "audio"]
            .contains(&key)
            .then(|| Agent {
                agent_id: key.into(),
                session: key.into(),
                is_janitor: true,
                backend: "codex".into(),
                model: "m".into(),
                ..Agent::default()
            })
    }
    fn zone(&self, _: &str) -> bool {
        false
    }
    fn draw(&mut self) -> String {
        unreachable!()
    }
    fn backend(&self, id: &str) -> bool {
        matches!(id, "claude" | "codex")
    }
    fn model(&self, _: &str, model: &str) -> bool {
        model != "invalid"
    }
    fn efforts(&self, _: &str) -> Vec<String> {
        vec![]
    }
    fn base_interval(&self, _: &str, _: &str, _: i64) -> i64 {
        unreachable!()
    }
    fn next_run(&self, _: &str, _: &str, _: i64) -> Option<i64> {
        unreachable!()
    }
    fn directory(&self, _: &str) -> String {
        unreachable!()
    }
    fn drawn(&mut self) -> String {
        unreachable!()
    }
    fn face(&self, _: &str) -> String {
        unreachable!()
    }
}

#[test]
fn isolated_reference_policy_calls_and_replies() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/model-fallbacks-vectors.json")).unwrap();
    use clarp_kernel::janitors::routing_input::{prompt_bytes, public_error, timeout};
    for case in vectors["encodings"].as_array().unwrap() {
        let codepoints: Vec<u32> = serde_json::from_value(case["points"].clone()).unwrap();
        match prompt_bytes(case["backend"].as_str().unwrap(), codepoints) {
            Ok(bytes) => assert_eq!(json!(bytes), case["bytes"]),
            Err(error) => assert_eq!(error, case["error"]),
        }
    }
    for case in vectors["timeouts"].as_array().unwrap() {
        assert_eq!(
            timeout(
                case["backend"].as_str().unwrap(),
                case["seconds"].as_f64().unwrap()
            ),
            case["expected"]
        );
    }
    for case in vectors["historical"].as_array().unwrap() {
        assert_eq!(
            public_error(case["stored"].as_str().unwrap()),
            case["expected"]
        );
    }
    for case in vectors["routing"].as_array().unwrap() {
        use clarp_kernel::janitors::routing_input::{Input, prepare};
        let count = case["count"].as_u64().unwrap() as usize;
        let prompt = if count == 0 {
            case["prompt"].as_str().unwrap().into()
        } else {
            case["unit"].as_str().unwrap().repeat(count)
        };
        let mut argv: Vec<_> = case["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().replace("@prompt", &prompt))
            .collect();
        match prepare(case["backend"].as_str().unwrap(), &mut argv, &prompt) {
            Err(error) => assert_eq!(error, case["error"], "{case}"),
            Ok(input) => {
                let name = match input {
                    Input::Arguments => "argv",
                    Input::Stdin => "stdin",
                    Input::File => {
                        argv.push("@file".into());
                        "file"
                    }
                };
                assert_eq!(name, case["where"], "{case}");
                let symbolic: Vec<_> = argv.iter().map(|v| v.replace(&prompt, "@prompt")).collect();
                assert_eq!(json!(symbolic), case["argv"], "{case}");
            }
        }
    }
    let mut book = Book::default();
    for (name, role, how) in [
        ("router", "message-delegator", "ephemeral"),
        ("keeper", "goal-keeper", "ephemeral"),
        ("scheduled", "scheduled-prompt", "ephemeral"),
        ("audio", "audio-bookkeeper", "deterministic"),
    ] {
        book.configs.insert(
            name.into(),
            Config {
                revision: 4,
                template_id: role.into(),
                execution:
                    json!({"executor":how,"provider":if name=="audio"{"local"}else{"codex"}})
                        .as_object()
                        .unwrap()
                        .clone(),
                ..Config::default()
            },
        );
    }
    for case in vectors["policy"].as_array().unwrap() {
        let result =
            book.model_policy
                .configure(&case["patch"], &case["revision"], &Host, &book.configs);
        let answer = match result {
            Ok(v) => json!([200, v]),
            Err(e) if e.code.is_empty() => json!([e.status,{"error":e.message}]),
            Err(e) => json!([e.status, e.told()]),
        };
        assert_eq!(answer, case["answer"], "{case}");
        assert_eq!(json!(book.model_policy), case["kept"]);
        for (name, cfg) in &book.configs {
            assert_eq!(
                book.model_policy
                    .effective(name, &Host.agent(name).unwrap(), cfg),
                case["effective"][name]
            );
        }
    }
    for case in vectors["failures"].as_array().unwrap() {
        assert_eq!(
            json!(fallbacks::retryable(case["text"].as_str().unwrap())),
            case["retry"],
            "{case}"
        );
    }
    for case in vectors["shapes"].as_array().unwrap() {
        let text = format!(
            "{}{}{}",
            case["before"].as_str().unwrap(),
            case["answer"].as_str().unwrap(),
            " ".repeat(case["pad"].as_u64().unwrap() as usize)
        );
        assert_eq!(
            json!(fallbacks::schema_object(
                &text,
                case.get("schema").unwrap_or(&vectors["schema"])
            )),
            case["expected"],
            "{case}"
        );
    }
    let primary = Pick {
        backend: "codex".into(),
        model: "a".into(),
        effort: String::new(),
    };
    for case in vectors["calls"].as_array().unwrap() {
        let chain = std::iter::once(json!({"provider":"codex","model":"a"}))
            .chain(
                case["models"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|m| json!({"provider":"codex","model":m})),
            )
            .collect::<Vec<_>>();
        let (first, tail) =
            fallbacks::choices(primary.clone(), &json!({"source":"global","chain":chain}));
        assert_eq!(first.model, case["tried"][0]);
        for receipt in case["receipts"].as_array().unwrap() {
            let index = receipt["attempt"].as_u64().unwrap() as usize;
            let actual = tail.iter().find(|(i, _)| *i == index).unwrap();
            assert_eq!(actual.1.model, receipt["model"]);
            assert_eq!(actual.1.backend, receipt["backend"]);
            assert_eq!(actual.1.effort, receipt["effort"]);
        }
    }
    for (key, expected) in [
        ("calls", 11),
        ("encodings", 60),
        ("failures", 14),
        ("historical", 8),
        ("policy", 30),
        ("routing", 86),
        ("shapes", 12),
        ("timeouts", 7),
    ] {
        assert_eq!(vectors[key].as_array().unwrap().len(), expected, "{key}");
    }
}
