use clarp_core::launch::parse;

fn args(line: &str) -> Vec<String> {
    line.split_whitespace().map(str::to_owned).collect()
}

#[test]
fn a_backend_starts_an_agent_on_its_own() {
    let options = parse(&args("--backend Codex --cwd /work --model gpt --effort high")).unwrap();
    assert_eq!((options.backend.as_str(), options.cwd.as_deref(), options.model.as_deref(), options.effort.as_deref()), ("codex", Some("/work"), Some("gpt"), Some("high")));
    assert!(options.explicit_agent_launch());
    let launch = options.launch_on_startup(false, false, false);
    assert!(launch && options.auto_start(launch), "an explicit backend auto-starts, whatever Settings say");
    assert_eq!(parse(&args("--backend=grok")).unwrap().backend, "grok");
}

#[test]
fn settings_decide_without_flags_and_restores_never_launch() {
    let plain = parse(&[]).unwrap();
    assert!(plain.launch_on_startup(false, true, false) && !plain.auto_start(true), "the hub opens; nothing starts by itself");
    assert!(!plain.launch_on_startup(false, true, true), "screenshot runs never launch from Settings");
    assert!(!plain.launch_on_startup(true, true, false), "a restored window never launches");
    let anonymous = parse(&args("--anonymous")).unwrap();
    assert_eq!(anonymous.anonymous_mode(), 1);
    assert_eq!(parse(&args("--contact")).unwrap().anonymous_mode(), 0);
    let empty = parse(&args("--no-new-agent")).unwrap();
    assert!(!empty.launch_on_startup(false, true, false) && empty.empty_startup(false) && !empty.empty_startup(true));
    assert!(!parse(&args("--preview-versions")).unwrap().launch_on_startup(false, true, false));
}

#[test]
fn conflicting_or_unknown_launch_flags_are_refused() {
    for line in ["--anonymous --contact", "--no-new-agent --backend claude", "--no-new-agent --cwd /x", "--backend vim"] {
        assert!(parse(&args(line)).is_err(), "{line}");
    }
    assert!(parse(&["--backend=".into()]).is_err(), "an empty backend is not a backend");
    assert!(parse(&args("--cwd")).unwrap_err().contains("Missing value"));
    assert!(parse(&args("--probe-store=/x --unknown")).is_ok(), "other arguments pass through");
    assert!(parse(&args("--help")).unwrap().help);
}
