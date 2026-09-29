use clarp_core::preview::{Finished, Launch, PreviewVersions, relaunch_arguments, relaunch_environment};

fn fixture() -> PreviewVersions {
    PreviewVersions::new(&Launch {
        home: "/home/u".into(),
        screenshot: true,
        screenshot_scenario: "preview-versions".into(),
        ..Launch::default()
    })
}

fn preview_window() -> PreviewVersions {
    let mut versions = PreviewVersions::new(&Launch {
        home: "/home/u".into(),
        instance_name: "com.maxteabag.Clarp.WorktreePreview".into(),
        helper_exists: true,
        executable_hash: "running".into(),
        ..Launch::default()
    });
    assert!(versions.finished(true, br#"{"current": "new", "versions": [{"hash": "new"}]}"#) == Finished::Nothing);
    versions.selected_host = "http://h".into();
    versions.selected_session = "s".into();
    versions
}

#[test]
fn preview_restart_captures_context_and_rejects_busy() {
    let mut versions = fixture();
    versions.selected_host = "http://origin.example".into();
    versions.selected_session = "exact-selected-session".into();
    versions.restart_allowed = false;
    assert_eq!(versions.select_version("new", false), None);
    assert_eq!(versions.restart_context(1)["session"], "");
    assert!(!versions.error().is_empty());
    versions.restart_allowed = true;
    assert_eq!(versions.select_version("new", false), None, "the fixture never runs the helper");
    let context = versions.restart_context(42);
    assert_eq!(context["session"], "exact-selected-session");
    assert_eq!(context["host"], "http://origin.example");
    assert!(context["arguments"].as_array().unwrap().iter().any(|a| a == "--restart-after"));
}

#[test]
fn relaunch_preserves_host_and_session() {
    let env = relaunch_environment("http://host.example:7682", "agent-exact");
    assert!(env.contains(&("CLARP_RESTORE_SESSION".into(), "agent-exact".into())));
    assert!(env.contains(&("CLARP_BASE_URL".into(), "http://host.example:7682".into())));
    assert!(env.contains(&("CLARP_RESTORE_DESKTOP".into(), "1".into())));
    assert_eq!(relaunch_arguments("/helper.py", 123), ["/helper.py", "--restart-after", "123"]);
}

#[test]
fn only_preview_instances_with_a_helper_are_enabled() {
    assert!(!PreviewVersions::new(&Launch { helper_exists: true, ..Launch::default() }).enabled());
    let no_helper = Launch { instance_name: "com.maxteabag.Clarp.WorktreePreview".into(), ..Launch::default() };
    assert!(!PreviewVersions::new(&no_helper).enabled());
    let screenshot = Launch { manager: true, helper_exists: true, screenshot: true, ..Launch::default() };
    assert!(!PreviewVersions::new(&screenshot).enabled(), "screenshots never run the real helper");
    let manager = PreviewVersions::new(&Launch { manager: true, helper_exists: true, executable_hash: "x".into(), ..Launch::default() });
    assert!(manager.enabled() && manager.running_hash().is_empty());
}

#[test]
fn helper_replies_drive_catalog_selection_and_relaunch() {
    let mut versions = preview_window();
    assert_eq!(versions.running_hash(), "running");
    assert_eq!(versions.refresh(true), None, "no second run while one is busy");
    assert_eq!(versions.refresh(false).unwrap()[1], "--catalog");
    let select = versions.select_version("new", false).unwrap();
    assert_eq!(select[1..], ["--select", "new", "--expected-current", "new"]);
    assert_eq!(versions.finished(true, br#"{"ok": true}"#), Finished::Relaunch);

    // The selection moved on while the helper ran: keep the window.
    versions.select_version("new", false).unwrap();
    versions.selected_session = "other".into();
    assert_eq!(versions.finished(true, br#"{"ok": true}"#), Finished::Nothing);
    assert!(versions.error().contains("update again"));

    versions.select_version("new", false).unwrap();
    assert_eq!(versions.finished(false, br#"{"error": "pin refused"}"#), Finished::Nothing);
    assert_eq!(versions.error(), "pin refused");
    assert_eq!(versions.finished(true, b"not json"), Finished::Nothing);
    assert_eq!(versions.error(), "Update action failed; the open window was kept.");
    // A failed refresh keeps the last catalog.
    assert_eq!(versions.catalog()["current"], "new");
}
