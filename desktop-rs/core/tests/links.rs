use clarp_core::links::{agent_terminal_command, local_report_path, terminal_command};

#[test]
fn the_terminal_runs_this_desktop_as_the_bridge_to_the_hosts_terminal() {
    let command = agent_terminal_command("/opt/clarp/clarp-slint", "rachel", "https://host.example");
    assert_eq!(command, ["/opt/clarp/clarp-slint", "--agent-terminal", "rachel", "--host", "https://host.example"]);
    let (launcher, arguments) = terminal_command(true, "Rachel — claude", "/work/project with spaces", &command);
    assert_eq!(launcher, "xdg-terminal-exec");
    assert_eq!(
        arguments,
        ["--dir=/work/project with spaces", "--title=Rachel — claude", "--", "/opt/clarp/clarp-slint", "--agent-terminal", "rachel", "--host", "https://host.example"]
    );
    let (fallback, arguments) = terminal_command(false, "t", "/w", &command);
    assert_eq!((fallback.as_str(), &arguments[..2]), ("x-terminal-emulator", &["-e".to_owned(), "/opt/clarp/clarp-slint".to_owned()][..]));
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
    assert!(local_report_path(&write("write-only.txt", b"text", 0o200)).is_none(), "write-only");
    // A link opens its target, by the target's canonical path.
    let alias = directory.join("alias.html");
    std::os::unix::fs::symlink(&report, &alias).unwrap();
    let canonical = std::fs::canonicalize(&report).unwrap();
    assert_eq!(local_report_path(&format!("file://{}", alias.display())), Some(canonical.clone()));
    assert!(local_report_path(&directory.to_string_lossy()).is_none(), "a directory");
    std::fs::remove_dir_all(directory).ok();
}
