use super::*;

#[test]
fn a_janitor_is_refused_before_any_move_and_its_neighbor_can_still_move() {
    let place = Place::new("move-janitors");
    let host = place.start();
    let refusal = "A Janitor cannot be moved. Use this Host's own built-in Janitors, or create a custom Janitor here from its definition.";
    let row = |session: &str| {
        json!({
            "agent_id":session,"session":session,"persona":session,"cwd":place.root.join("work"),
            "backend":"scripted","is_janitor":session=="maintenance"
        })
    };
    let roster = json!({"agents":[row("maintenance"),row("ordinary")],"personas":[]});
    let (source, requests) = answers(vec![
        ("/agents/snapshot", "200 OK", roster.to_string()),
        (
            "/log",
            "200 OK",
            json!({"turns":[{"id":"sample-row","role":"assistant","text":"History arrived"}]})
                .to_string(),
        ),
        ("/task-plans", "200 OK", json!({"plans":[]}).to_string()),
        ("/artifacts", "200 OK", json!({"artifacts":[]}).to_string()),
        ("/media", "200 OK", json!({"assets":[]}).to_string()),
        (
            "/agent-portraits",
            "200 OK",
            json!({"portraits":[]}).to_string(),
        ),
        ("/transcription-providers", "200 OK", "{}".to_string()),
    ]);
    let import = |session: &str, extra: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_clarp-host"))
            .args([
                "import",
                "--from",
                &source,
                "--host",
                &host.address,
                "--session",
                session,
            ])
            .args(extra)
            .env_remove("CLARP_SOURCE_TOKEN")
            .env_remove("CLARP_HOST_TOKEN")
            .ended()
    };
    let before = host.get("/agents/snapshot")["agents"].clone();
    for (i, extra) in [[].as_slice(), ["--files"].as_slice()]
        .into_iter()
        .enumerate()
    {
        let denied = import("maintenance", extra);
        assert!(!denied.status.success());
        assert!(
            String::from_utf8_lossy(&denied.stderr)
                .lines()
                .any(|line| line == format!("clarp-host import: {refusal}"))
        );
        let asked = requests.lock().unwrap();
        assert_eq!(asked.len(), i + 1);
        assert!(asked.iter().all(|r| r.starts_with("GET /agents/snapshot ")));
    }
    let direct =
        json!({"agent":row("maintenance"),"from":source,"turns":[],"picture":"not valid base64"});
    assert_eq!(
        host.ask("POST", "/agents/import", Some(&direct)),
        (409, json!({"error":refusal}))
    );
    assert_eq!(host.get("/agents/snapshot")["agents"], before);
    let moved = import("ordinary", &[]);
    assert!(
        moved.status.success(),
        "{}",
        String::from_utf8_lossy(&moved.stderr)
    );
    assert_eq!(
        host.conversation("ordinary"),
        [said("assistant", "History arrived")]
    );
    let after = host.get("/agents/snapshot");
    let rows = after["agents"].as_array().unwrap();
    assert_eq!(rows.len(), before.as_array().unwrap().len() + 1);
    assert!(!rows.iter().any(|a| a["session"] == "maintenance"));
}
