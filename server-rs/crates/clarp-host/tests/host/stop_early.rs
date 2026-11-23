//! The Host's tests of a Stop that comes while the turn's shim is still starting. One file a
//! piece, declared in `host.rs`.

use super::*;

/// Issue 722: a Stop that came before the turn's shim listened on its socket was lost. The Stop
/// answered that it had ended the turn, and the turn ran on to its end with its tool, while the
/// agent stayed at work. Under load a shim can take seconds to start; here it takes three on
/// purpose, and the Stop comes in between.
#[test]
fn a_stop_that_comes_before_the_shim_listens_ends_the_turn_once_it_does() {
    let place = Place::new("stop-early");
    let built = std::path::Path::new(env!("CARGO_BIN_EXE_clarp-host")).with_file_name("clarp-shim");
    let shim = place.root.join("slow-shim");
    std::fs::write(
        &shim,
        format!("#!/bin/sh\nsleep 3\nexec '{}' \"$@\"\n", built.display()),
    )
    .unwrap();
    std::fs::set_permissions(&shim, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let host = place.start_with("", &["--shim", shim.to_str().unwrap()]);
    let session = host.new_agent("Early");
    let agent_id = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == session)
        .map(|agent| agent["agent_id"].as_str().unwrap().to_string())
        .unwrap();
    let spool = place
        .root
        .join("data/agents")
        .join(&agent_id)
        .join("spool/1.jsonl");
    host.send(&session, "sleep 60", "m1");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    // The shim makes the turn's spool before anything else: without one, it is still starting.
    assert!(
        !spool.exists(),
        "the shim had started before the Stop: this test did not test what it is for"
    );
    let stopped = host.post("/stop", json!({ "session": session }));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    wait_within(
        "the turn to end after a Stop that came while its shim was starting",
        Duration::from_secs(20),
        || host.state(&session) == "interrupted",
    );
    // Nothing of the turn runs on: not its shim, not its tool, not what the tool started.
    let root = place.root.to_string_lossy().into_owned();
    let host_pid = host.process.id();
    let left = || -> Vec<String> {
        std::fs::read_dir("/proc")
            .unwrap()
            .flatten()
            .filter_map(|entry| {
                let pid = entry.file_name().to_str()?.parse::<u32>().ok()?;
                let stat = std::fs::read_to_string(entry.path().join("stat")).ok()?;
                let state = stat
                    .rsplit_once(')')?
                    .1
                    .split_whitespace()
                    .next()?
                    .to_string();
                let cmdline = std::fs::read(entry.path().join("cmdline")).ok()?;
                let cmdline = String::from_utf8_lossy(&cmdline).replace('\0', " ");
                let cwd = std::fs::read_link(entry.path().join("cwd")).unwrap_or_default();
                let ours = cmdline.contains(&root) || cwd.starts_with(&place.root);
                (ours && pid != host_pid && state != "Z").then(|| format!("{pid} {cmdline}"))
            })
            .collect()
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    while !left().is_empty() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    assert_eq!(
        left(),
        Vec::<String>::new(),
        "the turn's processes ran on after its end"
    );
}
