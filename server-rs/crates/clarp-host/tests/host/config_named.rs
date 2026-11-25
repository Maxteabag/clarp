//! A Host reads the owner's settings only from the folder it is named (issue 780): one file a
//! piece, declared in `host.rs`.

use super::*;

/// A Hotseat whose two Claude accounts are both spent, the soonest back in five days: the
/// owner's rule for a banked reset holds. Every call is written down beside it.
const HOTSEAT: &str = r#"#!/bin/sh
echo "$1" >> "$(dirname "$0")/calls"
now=$(date +%s)
soon=$((now + 3600)); week=$((now + 5 * 86400))
case "$1" in
list)
  printf '{"accounts":[{"alias":"one","is_active":true,"usage":{"used_5h":1.0,"reset_5h":%s,"used_7d":1.0,"reset_7d":%s}},{"alias":"two","usage":{"used_5h":1.0,"reset_5h":%s,"used_7d":1.0,"reset_7d":%s}}]}\n' "$soon" "$week" "$soon" "$week"
  ;;
codex) printf '%s\n' '{"accounts":[]}' ;;
*) exit 2 ;;
esac
"#;

/// A Host on a pool that is spent, in a place whose installed folder names a reset command.
/// Answers the Host and the file the command leaves when it runs.
fn spent_pool(name: &str, named: bool) -> (Place, Host, PathBuf) {
    let place = Place::new(name);
    let hotseat = place.root.join("hotseat");
    program(&hotseat, HOTSEAT);
    let ran = place.root.join("reset-ran");
    let reset = place.root.join("reset");
    program(
        &reset,
        &format!(
            "#!/bin/sh\ncat > /dev/null\n: > '{}'\nprintf '%s\\n' '{{\"status\":\"no_pending\"}}'\n",
            ran.display()
        ),
    );
    // The installed Host's folder, where its installer puts it in this place's home.
    let folder = sealed::home(&place.root).join(".config/clarp-host");
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::write(
        folder.join("config.toml"),
        format!("[agents]\nclaude_reset_command = [\"{}\"]\n", reset.display()),
    )
    .unwrap();
    let mut more = vec![
        "--accounts".to_string(),
        "select".to_string(),
        "--seats-tool".to_string(),
        hotseat.display().to_string(),
        "--janitors-checked-every-ms".to_string(),
        "100".to_string(),
    ];
    if named {
        more.extend(["--config".to_string(), folder.display().to_string()]);
    }
    let more: Vec<&str> = more.iter().map(String::as_str).collect();
    let host = place.start_in(
        "",
        &more,
        &[
            ("CLARP_CLAUDE_PROGRAM", "/bin/false"),
            ("CLARP_CODEX_PROGRAM", "/bin/false"),
        ],
    );
    // The switcher runs as the Hotseat janitor of an agent.
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
    (place, host, ran)
}

fn read_the_pool(place: &Place) {
    wait_for("the switcher to read the pool", || {
        std::fs::read_to_string(place.root.join("calls"))
            .is_ok_and(|calls| calls.contains("list"))
    });
}

#[test]
fn a_host_started_without_config_never_runs_the_reset_command_the_installed_folder_names() {
    // Named the folder, the Host runs the command on this pool: the case is one that spends.
    let (place, _host, ran) = spent_pool("config-named", true);
    read_the_pool(&place);
    wait_for("the named folder's reset command to run", || ran.exists());

    // Named none, the same pool and the same folder spend nothing.
    let (place, host, ran) = spent_pool("config-unnamed", false);
    read_the_pool(&place);
    wait_for("the Host to say it was named no folder", || {
        host.heard
            .lock()
            .unwrap()
            .contains("started without --config")
    });
    // The read and the decision on it are one pass; what it would run starts at once.
    std::thread::sleep(Duration::from_secs(3));
    assert!(!ran.exists(), "no reset command runs without --config");
}
