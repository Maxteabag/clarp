//! The Host's tests of what an agent's own tools read of it: the public fields of its peers,
//! the list and detail of its background jobs, and a job's status the Host cannot answer.
//! One file a piece, declared in `host.rs`.

use super::*;

#[test]
fn agent_read_parity_peers_list_uses_only_public_fields() {
    let place = Place::new("tool-peers-list");
    let admin = "fixture-admin";
    let peers = place.root.join("peers.json");
    let host = place.start_with(admin, &["--peers", peers.to_str().unwrap()]);
    let token = "private-fixture-peer-token";
    let peer = json!({
        "name": "work", "url": "https://work.invalid", "token": token, "server_id": "work-host",
    });
    host.post("/peers", peer.clone());

    let tools = clarp_host::tool_token(admin);
    let output = place
        .host_command()
        .args(["tool", "clarp-admin", "peers", "list"])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_TOOL_TOKEN", &tools)
        .env_remove("CLARP_HOST_TOKEN")
        .ended();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let listed: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        listed,
        json!([{"name": "work", "url": "https://work.invalid", "enabled": true, "server_id": "work-host"}])
    );
    for (method, path) in [
        ("GET", "/peers"),
        ("POST", "/peers"),
        ("DELETE", "/peers/work"),
    ] {
        assert_eq!(host.ask_as(&tools, method, path, Some(&peer)).0, 403);
    }
}

#[test]
fn agent_read_parity_background_jobs_list_and_detail() {
    let place = Place::new("tool-background-jobs");
    let admin = "fixture-admin";
    let host = place.start_with_token(admin);
    let session = host.new_agent("Worker");
    let job_id = "tool-list-job";
    let (code, _, error) = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            job_id,
            "implementation",
            "Fixture job",
        ],
    );
    assert_eq!(code, 0, "{error}");
    let tools = clarp_host::tool_token(admin);
    let (status, listed) = host.ask_as(&tools, "GET", "/background-jobs", None);
    assert_eq!(status, 200, "{listed}");
    let jobs = listed["jobs"].as_array().unwrap();
    let job = jobs.iter().find(|job| job["job_id"] == job_id).unwrap();
    assert_eq!(job["session"], session);
    assert!(job.get("log_tail").is_none());
    let (status, detail) = host.ask_as(&tools, "GET", "/background-jobs/tool-list-job", None);
    assert_eq!(status, 200, "{detail}");
}

#[test]
fn job_active_reports_unknown_for_missing_jobs_and_unavailable_answers() {
    let place = Place::new("job-active-unknown");
    let host = place.start();
    let session = host.new_agent("Builder");
    let query = ["clarp-agent-bg", &session, "job-active", "bg1:1:unseen"];
    let missing = tool(&host, &place.root, None, &query);
    assert_eq!(missing.0, 2, "{}", missing.2);

    let address = host.address.clone();
    let token = host.token.clone();
    host.kill();
    let probe = |address: &str| {
        place
            .command(env!("CARGO_BIN_EXE_clarp-host"))
            .arg("tool")
            .args(query)
            .env("CLARP_HOST_ADDR", address)
            .env("CLARP_HOST_TOKEN", &token)
            .ended()
    };
    let unreachable = probe(&address);
    assert_eq!(unreachable.status.code(), Some(2), "{unreachable:?}");
    assert!(String::from_utf8_lossy(&unreachable.stderr).contains("does not answer"));

    let (url, _) = speech_reply_stand_in("200 OK", "{}");
    let address = url.strip_suffix("/v1/speech-to-text").unwrap();
    let unreadable = probe(address);
    assert_eq!(unreadable.status.code(), Some(2), "{unreadable:?}");
    assert!(String::from_utf8_lossy(&unreadable.stderr).contains("no valid job state"));
}
