#![allow(clippy::unwrap_used)]
use clarp_kernel::vocabulary::{self, Budget, Pack};
use serde_json::{Value, json};

#[test]
fn budgets_and_compilations_match_the_python_reference() {
    let v: Value =
        serde_json::from_str(include_str!("fixtures/vocabulary-packs-vectors.json")).unwrap();
    assert_eq!(json!(*vocabulary::BUDGETS), v["declared"]);
    for case in v["measured"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        assert_eq!(case["tokens"], vocabulary::estimate_tokens(text));
        assert_eq!(case["rarity"], vocabulary::rarity(text));
    }
    for case in v["budgets"].as_array().unwrap() {
        assert_eq!(
            case["result"],
            json!(vocabulary::budget_for(
                case["provider"].as_str().unwrap(),
                case["model"].as_str().unwrap()
            ))
        );
    }
    for case in v["stories"].as_array().unwrap() {
        let budget: Budget = serde_json::from_value(case["budget"].clone()).unwrap();
        let packs: Vec<Pack> = serde_json::from_value(case["packs"].clone()).unwrap();
        assert_eq!(
            case["audit"],
            vocabulary::compile(&packs, &budget),
            "{case}"
        );
    }
    for case in v["sources"].as_array().unwrap(){
        let sources:vocabulary::sources::Sources=serde_json::from_value(case["input"].clone()).unwrap();
        assert_eq!(json!(sources.packs()),case["packs"]);
    }
    for case in v["workspaces"].as_array().unwrap(){
        let paths:Vec<String>=serde_json::from_value(case["paths"].clone()).unwrap();
        assert_eq!(json!(vocabulary::sources::identifiers(&paths)),case["ids"]);
    }
    for case in v["verdicts"].as_array().unwrap(){
        let text=case["transcript"].as_str().unwrap();
        assert_eq!(case["discard"],vocabulary::judged_text(text,case["probability"].as_f64()).is_empty());
    }

}
