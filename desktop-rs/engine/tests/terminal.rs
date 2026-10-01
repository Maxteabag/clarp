//! The agent's native CLI in the default terminal, recorded through
//! `CLARP_TEST_TERMINAL_LOG` instead of started. The only test in this
//! binary: it sets the process environment before any other thread reads it.

mod common;

use std::os::unix::fs::PermissionsExt;

use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_native_cli_opens_in_the_agents_folder_only_on_a_shared_filesystem() {
    let host = Host::start("terminal");
    let bin = host.dir.join("bin");
    let workspace = host.dir.join("project with spaces");
    std::fs::create_dir_all(&bin).unwrap();
    std::fs::create_dir_all(&workspace).unwrap();
    for program in ["claude", "xdg-terminal-exec"] {
        let path = bin.join(program);
        // Never run: the launcher only records the command.
        std::fs::write(&path, "#!/bin/sh\nexit 1\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
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

    d.engine.open_agent_terminal("rachel");
    assert_eq!(d.engine.error(), "The native CLI requires this desktop and Host to share the local filesystem");
    assert!(!log.exists());
    d.engine.clear_error();

    d.engine.set_shared_filesystem(true);
    d.engine.open_agent_terminal("mike");
    assert_eq!(d.engine.error(), "codex is not installed on this desktop");
    d.engine.clear_error();
    d.engine.open_agent_terminal("rachel");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    let recorded: serde_json::Value = serde_json::from_str(std::fs::read_to_string(&log).unwrap().trim()).unwrap();
    let directory = workspace.to_str().unwrap();
    assert_eq!(recorded["program"], "xdg-terminal-exec");
    assert_eq!(recorded["directory"], directory);
    assert_eq!(
        recorded["arguments"],
        json!([format!("--dir={directory}"), "--title=Rachel — claude", "--", "env", "-u", "CLARP_TOKEN", "CLAUDE_PWA_SESSION=rachel",
               bin.join("claude").to_str().unwrap(), "--resume", "native-1"])
    );
}
