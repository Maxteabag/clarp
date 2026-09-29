use clarp_core::links::{local_report_path, native_terminal_launch, terminal_command};
use clarp_core::protocol::Agent;

fn agent(backend: &str, conversation: &str) -> Agent {
    Agent { backend: backend.into(), conversation_id: conversation.into(), ..Agent::default() }
}

#[test]
fn each_backend_resumes_its_native_conversation() {
    for (backend, program, flag) in [("claude", "claude", "--resume"), ("codex", "codex", "resume"), ("agy", "agy", "--conversation"), ("grok", "grok", "--resume")] {
        let (found, arguments) = native_terminal_launch(&agent(backend, "native-session;literal")).unwrap();
        assert_eq!((found.as_str(), arguments), (program, vec![flag.to_owned(), "native-session;literal".to_owned()]));
    }
    assert!(native_terminal_launch(&agent("claude", "")).unwrap_err().contains("Send a chat message first"));
    assert!(native_terminal_launch(&agent("claude", "-rf")).is_err(), "never an option-like id");
    assert!(native_terminal_launch(&agent("opencode", "x")).unwrap_err().contains("does not have a supported"));
}

#[test]
fn the_terminal_runs_the_cli_without_the_host_token() {
    let (launcher, arguments) = terminal_command(true, "agent", "Agent — codex", "/work/project with spaces", "/bin/codex", &["resume".into(), "id".into()]);
    assert_eq!(launcher, "xdg-terminal-exec");
    assert_eq!(arguments, ["--dir=/work/project with spaces", "--title=Agent — codex", "--", "env", "-u", "CLARP_TOKEN", "CLAUDE_PWA_SESSION=agent", "/bin/codex", "resume", "id"]);
    let (fallback, arguments) = terminal_command(false, "agent", "t", "/w", "/bin/codex", &[]);
    assert_eq!((fallback.as_str(), &arguments[..2]), ("x-terminal-emulator", &["-e".to_owned(), "env".to_owned()][..]));
}

#[test]
fn only_readable_non_executable_reports_open() {
    use std::os::unix::fs::PermissionsExt;
    let directory = std::env::temp_dir().join(format!("clarp-reports-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let write = |name: &str, bytes: &[u8], mode: u32| {
        let path = directory.join(name);
        std::fs::write(&path, bytes).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        path.to_string_lossy().into_owned()
    };
    let report = write("report.html", b"<html><body>ok</body></html>", 0o600);
    assert!(local_report_path(&report).is_some());
    assert!(local_report_path(&format!("file://{report}")).is_some());
    assert!(local_report_path(&format!("file://{report}?x=1")).is_none(), "no query");
    assert!(local_report_path(&format!("file://elsewhere{report}")).is_none(), "no remote host");
    assert!(local_report_path(&write("run.html", b"<html>", 0o700)).is_none(), "executable");
    assert!(local_report_path(&write("script.txt", b"#!/bin/sh\nrm -rf ~", 0o600)).is_none(), "a script by content");
    assert!(local_report_path(&write("app.txt", b"[Desktop Entry]\nExec=x", 0o600)).is_none(), "a desktop entry");
    assert!(local_report_path(&write("tool.sh", b"echo hi", 0o600)).is_none(), "unlisted extension");
    assert!(local_report_path(&write("fake.png", b"not a png", 0o600)).is_none(), "content must match");
    assert!(local_report_path(&write("real.png", b"\x89PNG\r\n\x1a\nrest", 0o600)).is_some());
    assert!(local_report_path(&write("locked.txt", b"secret", 0o000)).is_none(), "no read bits");
    assert!(local_report_path("/definitely/not/there.html").is_none());
    std::fs::remove_dir_all(directory).ok();
}
