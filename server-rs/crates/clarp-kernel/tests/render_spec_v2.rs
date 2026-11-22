#![allow(clippy::unwrap_used)]

use clarp_kernel::artifacts::render_spec::{self as form, Admitted, Catalog, J};
use serde_json::{Value, json};

fn payload(kind: &str, props: Value) -> Value {
    json!({
        "version": "example",
        "catalog": "clarp.form",
        "catalog_version": 2,
        "answer_schema": {"type": "object", "properties": {}},
        "submit": {"label": "Send", "intent": "send"},
        "spec": {"root": "root", "state": {}, "elements": {
            "root": {"type": kind, "props": props, "children": []}
        }}
    })
}

#[test]
fn chart_x_values_are_homogeneous_unique_and_ordered() {
    let v2 = Catalog::embedded_version(2).unwrap();
    let points = |x, second| {
        payload(
            "chart",
            json!({"kind": "line", "decimals": 2, "series": [
                {"name": "sample", "points": [{"x": x, "y": 25}, {"x": second, "y": 50}]}
            ]}),
        )
    };
    let good = points(json!(1), json!(2));
    assert_eq!(form::validate(&v2, &J::of(&good)), Ok(()));
    for (x, second, code) in [
        (json!(1), json!("two"), "chart_x_mixed"),
        (json!(2), json!(1), "chart_x_order"),
        (json!("one"), json!("one"), "chart_x_duplicate"),
    ] {
        let bad = form::validate(&v2, &J::of(&points(x, second))).unwrap_err();
        assert_eq!(bad.code, code);
        assert_eq!(bad.path, "/spec/elements/root/props/series/0/points/1/x");
    }
    let v1 = Catalog::embedded().unwrap();
    let mut old = good;
    old["catalog_version"] = json!(1);
    assert_eq!(
        form::validate(&v1, &J::of(&old)).unwrap_err().code,
        "component"
    );
}

#[test]
fn a_button_needs_an_action_and_uses_the_existing_admission() {
    let v2 = Catalog::embedded_version(2).unwrap();
    let mut button = payload("button", json!({"label": "Hold", "action": "hold"}));
    button["answer_schema"] = json!({
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["hold"]}},
        "required": ["action"],
    });
    assert_eq!(
        form::validate(&v2, &J::of(&button)).unwrap_err().code,
        "submit_with_buttons"
    );
    button.as_object_mut().unwrap().remove("submit");
    assert_eq!(form::validate(&v2, &J::of(&button)), Ok(()));
    let mut answer = json!({
        "submission_id": "button-submission-1",
        "version": "example",
        "answers": {"intent": "button", "state": {"action": "hold"}},
    });
    let admit =
        |answer: &Value| form::admit(&v2, "a", &J::of(&button), "ready", &J::of(answer), None);
    assert!(matches!(admit(&answer), Ok(Admitted::New { .. })));
    answer["answers"]["state"]["action"] = json!("ship");
    assert_eq!(admit(&answer).unwrap_err().code, "state_value");
    answer["answers"]["intent"] = json!("send");
    assert_eq!(admit(&answer).unwrap_err().code, "intent_mismatch");
}

#[test]
fn table_passes_decimal_metadata_and_refuses_values_outside_the_columns() {
    let v2 = Catalog::embedded_version(2).unwrap();
    let mut table = payload(
        "table",
        json!({
            "columns": [{"key": "time", "label": "Time", "decimals": 1}],
            "rows": [{"time": 125}, {"time": null}, {}],
        }),
    );
    assert_eq!(form::validate(&v2, &J::of(&table)), Ok(()));
    table["spec"]["elements"]["root"]["props"]["rows"][2]["extra"] = json!("unknown");
    assert_eq!(
        form::validate(&v2, &J::of(&table)).unwrap_err().code,
        "unknown_key"
    );
}

#[test]
fn v2_payloads_answers_and_client_levels_match_the_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/render-spec-vectors.json")).unwrap();
    let catalog = Catalog::embedded_version(2).unwrap();
    for case in vectors["form_v2"].as_array().unwrap() {
        let payload = J::of(&case["payload"]);
        match case["kind"].as_str().unwrap() {
            "payload" => match form::validate(&catalog, &payload) {
                Ok(()) => assert_eq!(
                    Value::Object(form::stored(&catalog, &payload).unwrap()),
                    case["stored"],
                    "{case}"
                ),
                Err(why) => assert_eq!(why.triple(Ok(&catalog)), case["refused"], "{case}"),
            },
            "answer" => {
                let artifact = &case["artifact"];
                let id = artifact["artifact_id"].as_str().unwrap();
                let title = artifact["title"].as_str().unwrap();
                let submitted = J::of(&case["answer"]);
                match form::admit(&catalog, id, &payload, "ready", &submitted, None) {
                    Ok(Admitted::New {
                        payload_hash,
                        answers_json,
                        synthesize_audio,
                    }) => {
                        assert!(synthesize_audio);
                        assert_eq!(
                            json!({"repeat": false, "payload_hash": payload_hash, "answers_json": answers_json}),
                            case["admitted"]
                        );
                        assert_eq!(
                            form::message_with_payload(
                                title,
                                id,
                                submitted.get("version").and_then(J::str).unwrap(),
                                submitted.get("submission_id").and_then(J::str).unwrap(),
                                &answers_json,
                                case["payload"].as_object().unwrap(),
                                submitted.get("answers").unwrap(),
                            ),
                            case["message"]
                        );
                    }
                    Ok(Admitted::Repeat) => panic!("a new submission was repeated"),
                    Err(why) => assert_eq!(why.triple(Ok(&catalog)), case["refused"], "{case}"),
                }
            }
            "answered_button" => {
                let goals = clarp_kernel::goals::GoalBook::default();
                let mut given = case["item"].clone();
                for (key, value) in [
                    ("agent_id", json!("fixture")),
                    ("session", json!("fixture")),
                    ("title", json!("Decide")),
                    ("summary", json!("")),
                    ("reference_id", json!("")),
                    ("created_at", json!(1)),
                    ("updated_at", json!(30)),
                    ("payload", case["payload"].clone()),
                    ("submissions", case["submissions"].clone()),
                ] {
                    given[key] = value;
                }
                let artifact: clarp_kernel::artifacts::Artifact =
                    serde_json::from_value(given).unwrap();
                let row = artifact.public("Fixture", &goals, 30);
                let fields: serde_json::Map<String, Value> = ["answered_action", "answered_label"]
                    .into_iter()
                    .filter_map(|key| row.get(key).cloned().map(|value| (key.to_string(), value)))
                    .collect();
                assert_eq!(Value::Object(fields), case["fields"], "{case}");
                assert!(row.get("payload").is_none(), "{case}");
            }
            "level" => {
                let table = |key: &str| -> Option<std::collections::BTreeMap<String, i64>> {
                    serde_json::from_value(case[key].clone()).unwrap()
                };
                assert_eq!(
                    json!(form::client_level(
                        case["header"].as_str(),
                        table("v1").as_ref(),
                        table("v2").as_ref(),
                    )),
                    case["level"]
                );
            }
            _ => panic!("unknown vector {case}"),
        }
    }
}
