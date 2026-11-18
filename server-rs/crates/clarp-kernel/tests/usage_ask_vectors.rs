//! Provider responses and refresh deadlines recorded by the Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::{
    quota::{Kept, Login, Tool},
    usage_ask::{agy_groups, agy_provider, due},
};
use serde_json::{Value, json};

#[test]
fn asks_when_the_reference_does_and_shows_every_model_group_bucket() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/usage-ask-vectors.json")).unwrap();
    let at = fixture["at"].as_i64().unwrap();
    let groups = agy_groups(&fixture["payload"]).unwrap();
    let mut count = 0;
    for case in fixture["cases"].as_array().unwrap() {
        let now = case["now"].as_i64().unwrap();
        assert_eq!(json!(due(Some(at), now)), case["due"]);
        assert_eq!(
            agy_provider("a-host:agy", &groups, at, now),
            case["provider"]
        );
        count += 1;
    }
    let mut kept = Kept::default();
    let login = Login {
        generation: "generation-one",
        account: Some("account-one"),
    };
    let tool = Tool {
        provider: "codex",
        instance: "a-host:codex",
    };
    let snapshot = json!({"windows":{"primary":{"used_percentage":100,"window_minutes":300,"resets_at":"2030-03-17T19:10:00Z"}}});
    let mut id = |p: &str| format!("{p}sample");
    kept.seen(tool, login, &snapshot, "/wham/usage", at, &mut id);
    let mut standing = clarp_kernel::standing::Standing::default();
    standing.seen(
        &json!({"five_hour":{"used_percentage":22}}),
        login.account,
        "anthropic-oauth-usage",
        at,
    );
    for case in fixture["ages"].as_array().unwrap() {
        let now = case["now"].as_i64().unwrap();
        let account = case["active"].as_str();
        if case["done"] == true {
            kept.completed(tool, Some(login), at + 1, &mut id);
        }
        assert_eq!(
            json!(kept.refresh_due(Some(Login { account, ..login }), now)),
            case["codex"],
            "{case}"
        );
        assert_eq!(
            json!(standing.refresh_due(account, None, now)),
            case["claude"],
            "{case}"
        );
        count += 1;
    }
    for case in fixture["agy"].as_array().unwrap() {
        let now=case["now"].as_i64().unwrap();
        let read=agy_groups(&case["body"]).map(|groups|agy_provider("a-host:agy",&groups,now,now));
        assert_eq!(json!(read),case["projected"]);
        count+=1;
    }
    assert_eq!(count, fixture["ages"].as_array().unwrap().len()+fixture["cases"].as_array().unwrap().len()+fixture["agy"].as_array().unwrap().len());
    println!("{count} projection and refresh steps");
}
