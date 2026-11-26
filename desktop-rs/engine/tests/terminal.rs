//! The agent's terminal in the default terminal, recorded through
//! `CLARP_TEST_TERMINAL_LOG` instead of started. The only test in this
//! binary: it sets the process environment before any other thread reads it.

mod common;

use std::os::unix::fs::PermissionsExt;

use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_terminal_opens_this_desktop_as_the_bridge_to_the_hosts_terminal() {
    let host = Host::start("terminal");
    let bin = host.dir.join("bin");
    let workspace = host.dir.join("project with spaces");
    std::fs::create_dir_all(&bin).unwrap();
    std::fs::create_dir_all(&workspace).unwrap();
    // The path the engine sees (macOS's temporary folder is a symlink).
    let workspace = workspace.canonicalize().unwrap();
    let path = bin.join("xdg-terminal-exec");
    // Never run: the launcher only records the command.
    std::fs::write(&path, "#!/bin/sh\nexit 1\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    let log = host.dir.join("terminal.jsonl");
    // SAFETY: the only test in this binary, before the engine starts threads.
    unsafe {
        std::env::set_var("PATH", &bin);
        std::env::set_var("CLARP_TEST_TERMINAL_LOG", &log);
        std::env::remove_var("CLARP_SHARED_FILESYSTEM_HOST");
    }
    host.control("/__control/agent", json!({"session": "rachel", "set": {"cwd": workspace, "conversation_id": "native-1"}}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel's folder", |e| e.roster().find("rachel").is_some_and(|a| a.working_directory == workspace.to_str().unwrap()));
    let bridge = std::env::current_exe().unwrap().to_string_lossy().into_owned();
    let recorded = || -> Vec<serde_json::Value> {
        std::fs::read_to_string(&log).unwrap_or_default().lines().map(|line| serde_json::from_str(line).unwrap()).collect()
    };

    // Without a shared filesystem too: the Host runs the CLI, so nothing of
    // the agent's needs to be on this desktop.
    d.engine.open_agent_terminal("rachel");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    let directory = workspace.to_str().unwrap();
    let opened = recorded();
    assert_eq!(opened.len(), 1);
    assert_eq!(opened[0]["program"], "xdg-terminal-exec");
    assert_eq!(opened[0]["directory"], directory);
    assert_eq!(
        opened[0]["arguments"],
        json!([format!("--dir={directory}"), "--title=Rachel · claude", "--", bridge, "--agent-terminal", "rachel", "--host", host.base])
    );

    // A backend whose CLI is not on this desktop opens all the same, in the
    // home folder when its folder is not here.
    d.engine.open_agent_terminal("mike");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    let opened = recorded();
    assert_eq!(opened.len(), 2);
    assert_eq!(opened[1]["arguments"].as_array().unwrap()[3..].to_vec(), [json!(bridge), json!("--agent-terminal"), json!("mike"), json!("--host"), json!(host.base)]);
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    assert_eq!(opened[1]["directory"], home);

    d.engine.open_agent_terminal("nobody");
    assert_eq!(recorded().len(), 2, "no terminal for a chat the Host does not list");
}
