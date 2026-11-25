//! The goal tool's body read from standard input (issue 768): one file a piece, declared in
//! `host.rs`.

use super::*;

/// Runs the goal tool as `tool` does, with `piped` on its standard input.
fn goal_piped(
    host: &Host,
    dir: &std::path::Path,
    args: &[&str],
    piped: &str,
) -> (i32, String, String) {
    let mut child = sealed_command(dir, env!("CARGO_BIN_EXE_clarp-host"))
        .arg("tool")
        .arg("clarp-goal")
        .args(args)
        .current_dir(dir)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_HOST_TOKEN", &host.token)
        .env_remove("CLARP_BACKGROUND_WORKER_PID")
        .env_remove("CLARP_SESSION")
        .env_remove("CLAUDE_PWA_SESSION")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    let piped = piped.to_string();
    // Written beside the reading, so a tool that answered early never blocks the writer.
    let writer = std::thread::spawn(move || {
        let _ = input.write_all(piped.as_bytes());
    });
    let out = child.wait_with_output().unwrap();
    writer.join().unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn a_checkpoint_past_what_one_argument_holds_is_piped_to_the_goal_tool_whole() {
    let place = Place::new("goal-stdin");
    let host = place.start();
    let session = host.new_agent("Planner");
    let made = goal(
        &host,
        &place.root,
        &[
            "create",
            &session,
            "long",
            "Fixture",
            "[]",
            r#"{"outcome":"Deliver","limits":"Local","criteria":["Done"]}"#,
        ],
    );
    let plan = made["plan_id"].as_str().unwrap().to_string();
    let revision = made["revision"].as_i64().unwrap().to_string();
    // Linux holds one argument to 128 KiB; this body is twice that and more.
    let next_work = "n".repeat(250 * 1024);
    let body = json!({"progress": "long", "next_work": next_work}).to_string();
    assert!(body.len() > 200 * 1024);
    let (code, out, err) = goal_piped(
        &host,
        &place.root,
        &["checkpoint", &plan, &revision, "-"],
        &format!("{body}\n"),
    );
    assert_eq!(code, 0, "{err}");
    let printed: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(printed["revision"], made["revision"].as_i64().unwrap() + 1);
    let kept = goal(&host, &place.root, &["get", &plan]);
    assert_eq!(kept["goal"]["checkpoint"]["next_work"], json!(next_work));
    assert_eq!(kept["goal"]["checkpoint"]["progress"], "long");

    // Two dashes are refused in the tool, and nothing reaches the Host.
    let now = kept["revision"].as_i64().unwrap().to_string();
    let (code, _, err) = goal_piped(&host, &place.root, &["act", &plan, &now, "-", "-"], "{}");
    assert_eq!(code, 2, "{err}");
    assert!(err.contains("only one argument may be -"), "{err}");
    assert_eq!(
        goal(&host, &place.root, &["get", &plan])["revision"],
        kept["revision"]
    );
}
