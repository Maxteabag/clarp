use super::*;

#[test]
fn hotseat_demand_includes_the_default_model_of_an_unconfigured_agent() {
    let place = Place::new("hotseat-demand-model");
    let hotseat = place.root.join("hotseat");
    program(
        &hotseat,
        r#"#!/bin/sh
case "$1" in
list)
  printf '%s\n' '{"accounts":[{"alias":"current","is_active":true,"usage":{"used_5h":0.3,"used_7d":0.1,"blocked_models":["Fable"]}},{"alias":"backup","usage":{"used_5h":0.4,"used_7d":0.1}}]}'
  ;;
codex) printf '%s\n' '{"accounts":[]}' ;;
switch)
  [ "$2" = backup ] || exit 2
  touch "$(dirname "$0")/switched"
  rm -f "$(dirname "$0")/out"
  printf '%s\n' '{"switched":true}'
  ;;
*) exit 2 ;;
esac
"#,
    );
    let (claude, out, _) = claude_that_is_out(&place);
    let owner_file = place.root.join("owners.toml");
    std::fs::write(&owner_file, "[agents]\nclaude_model = \"fable\"\n").unwrap();
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "select",
            "--seats-tool",
            hotseat.to_str().unwrap(),
            "--speech-keys",
            owner_file.to_str().unwrap(),
            "--janitors-checked-every-ms",
            "100",
        ],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", "/bin/false"),
            ("ANTHROPIC_MODEL", "fable"),
        ],
    );
    let worker = host.post(
        "/agents",
        json!({"name":"Default worker","backend":"claude"}),
    )["session"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        host.post("/agent-llm", json!({"session":worker,"model":""}))["model"],
        ""
    );
    std::fs::write(out, "").unwrap();
    host.send(&worker, "first turn on the default model", "demand-344");
    wait_for("the turn to park", || {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["session"] == worker && row["live_activity"]["state"] == "limited")
    });

    let scanner = host.post("/agents", json!({"name":"Scanner","backend":"codex"}))["session"]
        .as_str()
        .unwrap()
        .to_string();
    let job = host.post(
        "/janitors",
        json!({"session":scanner,"template_id":"account-hotseat"}),
    );
    let enabled = host.post(
        &format!("/janitors/{scanner}/enabled"),
        json!({"enabled":true,"expected_revision":job["janitor"]["revision"]}),
    );
    assert_eq!(enabled["janitor"]["enabled"], true);
    wait_for(
        "Hotseat to switch for Fable despite healthy general quota",
        || place.root.join("switched").exists(),
    );
    wait_for("the parked default-model turn to complete", || {
        host.state(&worker) == "done"
    });
}
