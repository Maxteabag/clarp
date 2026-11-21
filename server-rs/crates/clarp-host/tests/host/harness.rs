//! The Host's tests of the harness itself: what the tests of every piece lean on. One file a
//! piece, declared in `host.rs`.

use super::*;

/// A wait that cannot come true fails in the words of the Host the test started: its start
/// line, and what it said after it, so a timeout says why the Host did not do what was waited for.
#[test]
fn a_wait_that_times_out_says_what_the_host_under_test_said() {
    let place = Place::new("harness-wait");
    let host = place.start();
    let made = host.post(
        "/agents",
        json!({ "name": "Wren", "session": "wren", "backend": "scripted" }),
    );
    assert_eq!(made["session"], "wren", "{made}");
    let failed = std::panic::catch_unwind(|| {
        wait_within("what cannot come", Duration::from_millis(200), || false);
    });
    let payload = failed.expect_err("a wait that cannot come true fails");
    let said = payload
        .downcast_ref::<String>()
        .cloned()
        .unwrap_or_default();
    assert!(
        said.starts_with("timed out waiting for what cannot come\n"),
        "{said}"
    );
    let start = format!("clarp-host listens on {}", host.address);
    assert!(
        said.contains(&format!("the Host that said \"{start}\"")),
        "{said}"
    );
    // A wait that comes true says nothing.
    wait_within("what is there", Duration::from_millis(200), || true);
}

/// A machine that has everything a person's machine has, made inside `root`: a home with every
/// tool signed in and OpenCode's store and catalogue in it, every program a person installs on
/// its path (each leaves a mark under `marks` when it is run), every folder variable set, and a
/// provider's key in every variable the Host reads one from. Every value of it says `trap`, or
/// is under `root/the-machine`. Answers the machine's environment.
fn a_machine_that_has_everything(
    root: &std::path::Path,
) -> Vec<(std::ffi::OsString, std::ffi::OsString)> {
    let machine = root.join("the-machine");
    let home = machine.join("home");
    let bin = machine.join("bin");
    let marks = machine.join("marks");
    for dir in [&bin, &marks] {
        std::fs::create_dir_all(dir).unwrap();
    }
    let files = [
        (
            ".claude.json",
            r#"{"oauthAccount":{"emailAddress":"trap@example.invalid","accountUuid":"trap-account","organizationUuid":"trap-org"},"modelAccessCache":{"trap-model":true}}"#,
        ),
        (
            ".claude/.credentials.json",
            r#"{"claudeAiOauth":{"accessToken":"trap-token","refreshToken":"trap-token","expiresAt":4102444800000}}"#,
        ),
        (
            ".codex/auth.json",
            r#"{"OPENAI_API_KEY":null,"tokens":{"access_token":"trap-token","id_token":"trap-token","account_id":"trap-account"}}"#,
        ),
        (
            ".local/share/opencode/auth.json",
            r#"{"trapprovider":{"type":"api","key":"trap-key"},"openai":{"type":"api","key":"trap-key"}}"#,
        ),
        (
            ".cache/opencode/models.json",
            r#"{"trapprovider":{"name":"Trap Provider","env":["TRAP_PROVIDER_KEY"],"models":{"trap-model":{}}},"openai":{"name":"Trap OpenAI","env":["OPENAI_API_KEY"],"models":{"deepseek-trap":{}}},"deepgram":{"name":"Trap Deepgram","env":["DEEPGRAM_API_KEY"],"models":{}},"elevenlabs":{"name":"Trap ElevenLabs","env":["ELEVEN_API_KEY"],"models":{}},"google":{"name":"Trap Google","env":["GEMINI_API_KEY","GOOGLE_VERTEX_API_KEY"],"models":{}},"cartesia":{"name":"Trap Cartesia","env":["CARTESIA_API_KEY"],"models":{}}}"#,
        ),
        (
            ".local/share/muse/model-catalog/trap.json",
            r#"{"rows":[{"id":"trap-model","label":"Trap"}]}"#,
        ),
        (
            ".config/clarp-host/config.toml",
            "[server]\nkind = \"clarp-host\"\nbind_addr = \"127.0.0.1\"\nport = 9\nauth_token = \"trap-token\"\n",
        ),
    ];
    for (name, text) in files {
        let file = home.join(name);
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(file, text).unwrap();
    }
    let mut runner: Vec<(String, std::ffi::OsString)> = Vec::new();
    for (variable, name) in sealed::programs() {
        let tool = bin.join(name);
        program(
            &tool,
            &format!(
                "#!/bin/sh\n: > '{}'\necho trap-version\n",
                marks.join(name).display()
            ),
        );
        // A machine that names its tools by their variables, as well as having them on its path.
        runner.push((variable.to_string(), tool.into()));
    }
    let mut set = |name: &str, value: std::ffi::OsString| runner.push((name.to_string(), value));
    set("HOME", home.clone().into());
    set("PATH", format!("{}:/usr/bin:/bin", bin.display()).into());
    set("XDG_CONFIG_HOME", home.join(".config").into());
    set("XDG_DATA_HOME", home.join(".local/share").into());
    set("XDG_STATE_HOME", home.join(".local/state").into());
    set("XDG_CACHE_HOME", home.join(".cache").into());
    set("XDG_RUNTIME_DIR", machine.join("run").into());
    set("TMPDIR", machine.join("tmp").into());
    set("SHELL", bin.join("claude").into());
    set("CLAUDE_CONFIG_DIR", home.join(".claude").into());
    set("CODEX_HOME", home.join(".codex").into());
    set(
        "CLAUDE_PWA_OPENCODE_HOME",
        home.join(".local/share/opencode").into(),
    );
    set("CLAUDE_PWA_GROK_HOME", home.join(".grok").into());
    set(
        "CLAUDE_PWA_MUSE_HOME",
        home.join(".local/share/muse").into(),
    );
    set(
        "CLAUDE_PWA_CONFIG",
        home.join(".config/clarp-host/config.toml").into(),
    );
    set("CLARP_CONFIG_DIR", home.join(".config/clarp-host").into());
    set("TRAP_PROVIDER_KEY", "trap-key".into());
    set("LANG", "trap-LANG".into());
    set("TZ", "trap-TZ".into());
    // Every other variable the Host reads: a key, a token, a switch or a place, each a trap.
    for (name, _) in sealed::ABSENT {
        if !runner.iter().any(|(set, _)| set == name) {
            runner.push((name.to_string(), format!("trap-{name}").into()));
        }
    }
    runner
        .into_iter()
        .map(|(name, value)| (name.into(), value))
        .collect()
}

/// The rule itself: of a machine full of traps, a process a test starts is given nothing; all
/// it has is what the rule sets inside the test's own place, and its constants.
#[test]
fn what_a_test_gives_a_process_holds_nothing_of_a_machine_full_of_traps() {
    let place = Place::new("sealed-rule");
    let runner = a_machine_that_has_everything(&place.root);
    let machine = place.root.join("the-machine");
    let given = sealed::sealed(&runner, &place.root, &[]);
    let constants = [sealed::PATH, clarp_sealed::LANG, clarp_sealed::TZ];
    for (name, value) in &given {
        let shown = format!("{}={}", name.to_string_lossy(), value.to_string_lossy());
        let value = std::path::Path::new(value);
        assert!(
            !value.starts_with(&machine) && !value.to_string_lossy().contains("trap"),
            "of the machine: {shown}"
        );
        assert!(
            value.starts_with(&place.root)
                || constants
                    .iter()
                    .any(|constant| value == std::path::Path::new(constant)),
            "neither in the test's place nor a constant of the rule: {shown}"
        );
    }
    let at = |name: &str| given.get(std::ffi::OsStr::new(name)).cloned();
    let home = sealed::home(&place.root);
    assert_eq!(at("HOME"), Some(home.clone().into()));
    for (name, under) in [
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ] {
        assert_eq!(at(name), Some(home.join(under).into()), "{name}");
    }
    assert_eq!(at("PATH"), Some(sealed::PATH.into()));
    assert_eq!(at("LANG"), Some(clarp_sealed::LANG.into()));
    assert_eq!(at("TZ"), Some(clarp_sealed::TZ.into()));
    assert_eq!(
        at("XDG_RUNTIME_DIR"),
        Some(place.root.join("user-run").into())
    );
    assert_eq!(at("TMPDIR"), Some(place.root.join("tmp").into()));
    // Every program a person installs is a path that is not there.
    let programs = sealed::programs();
    for (variable, name) in &programs {
        let pinned = sealed::absent(&place.root).join(name);
        assert_eq!(at(variable), Some(pinned.clone().into()), "{variable}");
        assert!(!pinned.exists(), "{variable}: {}", pinned.display());
    }
    // Nothing the rule does not set: no key, no token, no folder of a tool, no switch.
    for (name, why) in sealed::ABSENT {
        assert_eq!(at(name), None, "{name} ({why}) reached the process");
    }
    assert_eq!(at("TRAP_PROVIDER_KEY"), None);
    assert_eq!(given.len(), 10 + programs.len(), "{given:?}");
    // What a test names on purpose is the runner's, and only that; a name the runner lacks
    // is not given at all.
    let named = sealed::sealed(
        &runner,
        &place.root,
        &["HOME", "NOT_ON_THE_MACHINE", "LANG"],
    );
    let named_at = |name: &str| named.get(std::ffi::OsStr::new(name)).cloned();
    assert_eq!(named_at("HOME"), Some(machine.join("home").into()));
    assert_eq!(named_at("NOT_ON_THE_MACHINE"), None);
    assert_eq!(named_at("LANG"), Some("trap-LANG".into()));
    assert_eq!(named_at("CODEX_HOME"), None);
    assert_eq!(named.len(), given.len(), "{named:?}");
    // A name the test takes and the runner lacks is not given at all, not even the default.
    let lacking = sealed::sealed(&[], &place.root, &["HOME"]);
    assert_eq!(lacking.get(std::ffi::OsStr::new("HOME")), None);
    assert_eq!(lacking.len(), given.len() - 1, "{lacking:?}");
}

/// What a process the harness starts really sees is the rule and nothing more: the runner's own
/// environment (cargo's variables, the shell's) is not inherited under it.
#[test]
fn a_process_a_test_starts_sees_the_rule_and_nothing_else() {
    let place = Place::new("sealed-seen");
    let seen = place.command("/usr/bin/env").arg("-0").output().unwrap();
    assert!(seen.status.success());
    let seen: std::collections::BTreeMap<String, String> = seen
        .stdout
        .split(|byte| *byte == 0)
        .filter(|line| !line.is_empty())
        .map(|line| {
            let line = String::from_utf8_lossy(line);
            let (name, value) = line.split_once('=').unwrap();
            (name.to_string(), value.to_string())
        })
        .collect();
    let rule: std::collections::BTreeMap<String, String> =
        sealed::sealed(&sealed::runner(), &place.root, &[])
            .into_iter()
            .map(|(name, value)| {
                (
                    name.to_string_lossy().into_owned(),
                    value.to_string_lossy().into_owned(),
                )
            })
            .collect();
    let more: Vec<&String> = seen
        .keys()
        .filter(|name| !rule.contains_key(*name))
        .collect();
    assert!(
        more.is_empty(),
        "a process a test starts sees more than the rule: {more:?}"
    );
    assert_eq!(seen, rule);
}

/// A Host a test starts on a machine that has everything sees none of it: it finds no tool and
/// runs none, reads no login, no store of OpenCode's, no key, and asks no provider of an
/// account. The machine is made by the test (the runner may have none of it); a runner that
/// has it all, as the build machine does, is held by the same rule.
#[test]
fn a_test_host_on_a_machine_that_has_everything_sees_none_of_it() {
    let place = Place::new("sealed-host");
    let runner = a_machine_that_has_everything(&place.root);
    let machine = place.root.join("the-machine");
    // Where a provider's usage would be asked: a stand-in on this machine that counts who came.
    let usage = TcpListener::bind("127.0.0.1:0").unwrap();
    usage.set_nonblocking(true).unwrap();
    let usage_at = usage.local_addr().unwrap().to_string();
    let world = [("CLARP_USAGE_LOOPBACK", usage_at.as_str())];
    let host = place.start_on(&runner, &[], "", &["--ask-usage"], &world, &[]);
    let of_the_machine = |what: &str, told: &Value| {
        let text = told.to_string();
        assert!(
            !text.contains("trap") && !text.contains(&*machine.to_string_lossy()),
            "{what} tells of the machine: {text}"
        );
    };

    let options = host.get("/agent-model-options");
    of_the_machine("/agent-model-options", &options);
    let tools = options["providers"].as_object().unwrap();
    assert!(!tools.is_empty(), "{options}");
    for (tool, row) in tools {
        assert_eq!(row["installed"], false, "{tool}: {row}");
        assert_eq!(row["cli_version"], Value::Null, "{tool}: {row}");
        assert_eq!(row["availability"], "unavailable", "{tool}: {row}");
    }

    let logins = host.get("/backend-auth?harnesses=all");
    of_the_machine("/backend-auth", &logins);
    let rows = logins["backends"]
        .as_array()
        .unwrap_or_else(|| panic!("{logins}"));
    assert!(!rows.is_empty(), "{logins}");
    for row in rows {
        assert_eq!(row["installed"], false, "{row}");
        assert_eq!(row["logged_in"], false, "{row}");
    }

    assert_eq!(
        host.get("/harness-providers?harness=opencode"),
        json!({
            "catalogue_available": false, "harness": "opencode", "installed": false,
            "providers": [], "store_harness": "opencode", "tasks": {},
        })
    );

    let used = host.get("/backend-usage");
    of_the_machine("/backend-usage", &used);
    // An engine that speaks has no key: the keys of the machine are not the Host's.
    let speech = host.get("/tts/providers");
    of_the_machine("/tts/providers", &speech);
    let engines = speech["providers"]
        .as_array()
        .unwrap_or_else(|| panic!("{speech}"));
    assert!(engines.len() > 1, "{speech}");
    for engine in engines.iter().filter(|engine| engine["id"] != "none") {
        assert_eq!(engine["available"], false, "{engine}");
    }
    // The conversation of an agent the Host does not have is told with the Host's home.
    let nobody = host.get("/log?session=nobody");
    of_the_machine("/log", &nobody);

    // Not one program of the machine's was run, and no provider was asked.
    let marks: Vec<_> = std::fs::read_dir(machine.join("marks"))
        .unwrap()
        .flatten()
        .map(|entry| entry.file_name())
        .collect();
    assert!(
        marks.is_empty(),
        "programs of the machine were run: {marks:?}"
    );
    assert!(
        usage.accept().is_err(),
        "a provider's usage was asked of the stand-in"
    );
}

/// Every tool a Host tells its apps of is in the product's table of programs, so the harness
/// keeps each from the machine; one that is not fails here by its name.
#[test]
fn every_tool_a_host_knows_has_a_program_the_harness_keeps_from_the_machine() {
    let place = Place::new("sealed-tools");
    let host = place.start();
    let options = host.get("/agent-model-options");
    let logins = host.get("/backend-auth?harnesses=all");
    let mut known: Vec<String> = options["providers"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    for row in logins["backends"].as_array().unwrap() {
        known.push(row["id"].as_str().unwrap().to_string());
    }
    known.sort();
    known.dedup();
    let pinned = sealed::sealed(&[], &place.root, &[]);
    let unkept: Vec<&String> = known
        .iter()
        .filter(|tool| {
            let program = clarp_backend::PROGRAMS.iter().find(|(id, _, _)| id == tool);
            !program
                .is_some_and(|(_, variable, _)| pinned.contains_key(std::ffi::OsStr::new(variable)))
        })
        .collect();
    assert!(
        unkept.is_empty(),
        "tools the Host knows that a test Host could find on the machine (no entry in \
         clarp_backend::PROGRAMS, or none pinned by tests/host/sealed.rs): {unkept:?}"
    );
}

/// Every variable the Host's crates read is one the harness decided on: set by the rule (inside
/// the test's place, or a constant), or absent unless a test names it (`clarp-sealed`). A new
/// variable read somewhere fails here by its name until it is decided on.
///
/// What the scan sees: in every source file of the workspace's crates that reads the environment
/// (`env::var`, `var_os`, `env::vars`), every string that is shaped like a variable's name
/// (capitals, digits and `_`, or a lower-case `*_proxy`), and the text of every constant such a
/// read names. What it cannot see: a name made at run time (OpenCode's catalogue names the
/// variables a provider's key is in), a name written in a file that does not itself read the
/// environment and handed to one that does, and what libraries read (proxies, certificates, a
/// time zone). Those are why the harness starts from nothing rather than from a list of names.
#[test]
fn every_variable_the_hosts_crates_read_is_one_the_harness_decided_on() {
    let crates = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut files = Vec::new();
    let mut dirs: Vec<std::path::PathBuf> = std::fs::read_dir(&crates)
        .unwrap()
        .flatten()
        .map(|entry| entry.path().join("src"))
        .filter(|dir| dir.is_dir())
        .collect();
    while let Some(dir) = dirs.pop() {
        for entry in std::fs::read_dir(&dir).unwrap().flatten() {
            let path = entry.path();
            if path.is_dir() {
                dirs.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                files.push((path.clone(), std::fs::read_to_string(&path).unwrap()));
            }
        }
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let shaped = |word: &str| {
        let upper = word.len() > 1
            && word.starts_with(|c: char| c.is_ascii_uppercase())
            && word
                .chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_');
        upper
            || (word.ends_with("_proxy")
                && word.chars().all(|c| c.is_ascii_lowercase() || c == '_'))
    };
    let strings = |text: &str| -> Vec<String> {
        literals(text)
            .into_iter()
            .filter(|word| shaped(word))
            .collect()
    };
    // The constants of the workspace that are text: by their names.
    let mut constants = std::collections::BTreeMap::new();
    for (_, text) in &files {
        for line in text.lines() {
            let line = line
                .trim_start()
                .trim_start_matches("pub ")
                .trim_start_matches("pub(crate) ");
            if let Some(rest) = line.strip_prefix("const ")
                && let Some((name, value)) = rest.split_once(": &str = \"")
                && let Some((value, _)) = value.split_once('"')
            {
                constants.insert(name.to_string(), value.to_string());
            }
        }
    }
    let reads = ["env::var(", "var_os(", "env::vars("];
    let mut read = std::collections::BTreeMap::<String, String>::new();
    for (path, text) in &files {
        if !reads.iter().any(|mark| text.contains(mark)) {
            continue;
        }
        let file = path.strip_prefix(&crates).unwrap().display().to_string();
        for name in strings(text) {
            read.entry(name).or_insert_with(|| file.clone());
        }
        // A read that names a constant: the constant's text.
        for mark in ["env::var(", "var_os("] {
            for after in text.split(mark).skip(1) {
                let named: String = after
                    .chars()
                    .take_while(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == ':')
                    .collect();
                let last = named.rsplit("::").next().unwrap_or_default();
                if let Some(value) = constants.get(last) {
                    read.entry(value.clone()).or_insert_with(|| file.clone());
                }
            }
        }
    }
    let set = sealed::sealed(&[], std::path::Path::new("/place"), &[]);
    let decided = |name: &str| {
        set.contains_key(std::ffi::OsStr::new(name))
            || sealed::ABSENT.iter().any(|(absent, _)| *absent == name)
            || sealed::NOT_READ.contains(&name)
    };
    let undecided: Vec<String> = read
        .iter()
        .filter(|(name, _)| !decided(name))
        .map(|(name, file)| format!("{name} (in {file})"))
        .collect();
    assert!(
        undecided.is_empty(),
        "variables the Host's crates read that clarp-sealed has not decided on (set it in the \
         test's place, or name it in ABSENT with why, or in NOT_READ): {undecided:?}"
    );
    // And the lists name nothing the crates no longer read.
    let stale: Vec<&str> = sealed::ABSENT
        .iter()
        .map(|(name, _)| *name)
        .chain(sealed::NOT_READ.iter().copied())
        .filter(|name| !read.contains_key(*name))
        .collect();
    assert!(
        stale.is_empty(),
        "named in clarp-sealed, read nowhere: {stale:?}"
    );
}

/// The Host's own tool, run where nobody said which Host it speaks to, on a machine that has an
/// installed Host (its file of settings names an address, where a trap listens, and a token):
/// it is not the machine's Host the test reaches. Every way the tests call a tool without an
/// address refuses in words, and the trap hears nothing.
#[test]
fn a_tool_with_no_address_on_a_machine_with_an_installed_host_reaches_nothing_of_it() {
    let place = Place::new("sealed-installed");
    let runner = a_machine_that_has_everything(&place.root);
    let trap = TcpListener::bind("127.0.0.1:0").unwrap();
    trap.set_nonblocking(true).unwrap();
    let port = trap.local_addr().unwrap().port();
    // The file an installation writes, wherever the machine says it is.
    let installed = format!(
        "[server]\nkind = \"clarp-host\"\nbind_addr = \"127.0.0.1\"\nport = {port}\nauth_token = \"trap-token\"\n"
    );
    let home = place.root.join("the-machine/home");
    std::fs::write(home.join(".config/clarp-host/config.toml"), &installed).unwrap();
    for (name, value) in &runner {
        if name == "CLAUDE_PWA_CONFIG" {
            std::fs::write(value, &installed).unwrap();
        }
    }
    for (tool, words) in [
        ("clarp-admin", &["sessions"][..]),
        ("clarp-agent-bg", &["list"][..]),
        (
            "clarp-goal",
            &["bookkeeping", "observe", "dg0123456789abcdef"][..],
        ),
    ] {
        let out = sealed_command_on(&runner, &place.root, &[], env!("CARGO_BIN_EXE_clarp-host"))
            .arg("tool")
            .arg(tool)
            .args(words)
            .current_dir(&place.root)
            .ended();
        let said = String::from_utf8_lossy(&out.stderr).into_owned();
        assert_eq!(out.status.code(), Some(69), "{tool}: {said}");
        assert!(
            said.contains("this agent's Host is not known") && said.contains("Nothing was done"),
            "{tool}: {said}"
        );
        assert!(!said.contains("trap"), "{tool}: {said}");
    }
    assert!(
        trap.accept().is_err(),
        "the machine's installed Host was reached"
    );
}

/// The commands of the Host's that speak to a running Host and, told no address, take a fixed
/// one (`127.0.0.1:7690`), which no variable of the rule moves: on a runner with a Host of its
/// own on that port, a test that leaves the address out speaks to that Host.
const AT_A_FIXED_ADDRESS: &[&str] = &["import", "peer", "network", "pair"];

/// Every start of one of those commands in `text`: its line, the command, and whether it names
/// `--host`. A start is a call `.args([` or `.arg(` whose first word is the command; it is read
/// to the end of its statement (the `;` after it, or the bracket that closes around it, as in a
/// closure that answers the command), so an address given in a later `.args(` of the same chain
/// counts.
fn starts_at_a_fixed_address(text: &str) -> Vec<(usize, String, bool)> {
    let read = tokens(text);
    let mut starts = Vec::new();
    for at in 0..read.len() {
        let command = match &read[at..read.len().min(at + 5)] {
            [
                (_, Token::Mark('.')),
                (_, Token::Word(call)),
                (_, Token::Mark('(')),
                (_, Token::Mark('[')),
                (_, Token::Text(first)),
            ] if call == "args" => first,
            [
                (_, Token::Mark('.')),
                (_, Token::Word(call)),
                (_, Token::Mark('(')),
                (_, Token::Text(first)),
                ..,
            ] if call == "arg" => first,
            _ => continue,
        };
        if !AT_A_FIXED_ADDRESS.contains(&command.as_str()) {
            continue;
        }
        let mut depth = 0i32;
        let mut named = false;
        for (_, token) in &read[at..] {
            match token {
                Token::Mark('(' | '[' | '{') => depth += 1,
                Token::Mark(')' | ']' | '}') => {
                    depth -= 1;
                    if depth < 0 {
                        break;
                    }
                }
                Token::Mark(';') if depth == 0 => break,
                Token::Text(word) if word == "--host" => named = true,
                _ => {}
            }
        }
        let line = text.chars().take(read[at].0).filter(|c| *c == '\n').count() + 1;
        starts.push((line, command.clone(), named));
    }
    starts
}

/// Every start of `import`, `peer`, `network` or `pair` in the Host's tests names the Host it
/// speaks to with `--host`. Told none, each takes a fixed address that no variable of the rule
/// moves, and on a runner whose own Host listens there (a person's machine does) the test
/// would speak to that Host, with whatever the test sends.
///
/// What the scan sees: in every source file under this crate's `tests`, a call `.args([` or
/// `.arg(` whose first word is one of the four, read to the end of its statement. What it
/// cannot see: a command whose word comes from a variable or a function, and a test that hands
/// its arguments to a helper elsewhere; such a start is written so the scan sees it.
#[test]
fn every_start_of_a_command_at_a_fixed_address_in_the_tests_names_its_host() {
    // The scan reads a start to its end, past what is no start.
    let sample = r#####"
        let a = place.host_command().args(["import", "--from", x]).args(["--host", y]).ended();
        let b = place.host_command().args(["peer", "list"]).ended(); // .args(["pair"])
        let c = |more: &[&str]| { place.host_command().arg("network").args(more).ended() };
        let d = place.host_command().args(["pair", "--relay"]).ended(); let e = ["--host"];
        let f = place.host_command().args(["tool", "import"]).ended();
        let g = "import";
    "#####;
    assert_eq!(
        starts_at_a_fixed_address(sample),
        [
            (2, "import".to_string(), true),
            (3, "peer".to_string(), false),
            (4, "network".to_string(), false),
            (5, "pair".to_string(), false),
        ]
    );
    let tests = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    let mut dirs = vec![tests.clone()];
    let mut found = 0;
    let mut unnamed = Vec::new();
    while let Some(dir) = dirs.pop() {
        for entry in std::fs::read_dir(&dir).unwrap().flatten() {
            let path = entry.path();
            if path.is_dir() {
                dirs.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                let text = std::fs::read_to_string(&path).unwrap();
                for (line, command, named) in starts_at_a_fixed_address(&text) {
                    found += 1;
                    if !named {
                        let file = path.strip_prefix(&tests).unwrap().display();
                        unnamed.push(format!("{command} at {file}:{line}"));
                    }
                }
            }
        }
    }
    assert!(
        unnamed.is_empty(),
        "starts of a command that, told no Host, speaks to whatever listens at 127.0.0.1:7690 \
         on the runner: give each --host {unnamed:?}"
    );
    // The tests had thirteen starts when the scan was written: fewer is a scan that reads none.
    assert!(found >= 13, "the scan found {found} starts");
}

/// These integration tests all start their processes through the shared environment rule.
#[test]
fn integration_tests_start_processes_with_the_shared_environment() {
    let tests = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    let mut files = vec![
        tests.join("host.rs"),
        tests.join("host"),
        tests.join("../../clarp-shim/tests/shim.rs"),
    ];
    let mut direct = Vec::new();
    while let Some(file) = files.pop() {
        if file.is_dir() {
            files.extend(
                std::fs::read_dir(&file)
                    .unwrap()
                    .map(|entry| entry.unwrap().path()),
            );
        } else if file.extension().is_some_and(|ext| ext == "rs") {
            let source = std::fs::read_to_string(&file).unwrap();
            for window in tokens(&source).windows(4) {
                if let [
                    (at, Token::Word(ty)),
                    (_, Token::Mark(':')),
                    (_, Token::Mark(':')),
                    (_, Token::Word(method)),
                ] = window
                    && ty == "Command"
                    && method == "new"
                {
                    let line = source.chars().take(*at).filter(|c| *c == '\n').count() + 1;
                    direct.push(format!("{}:{line}", file.display()));
                }
            }
        }
    }
    assert!(
        direct.is_empty(),
        "direct process constructors bypass the test environment: {direct:?}"
    );
}

/// What the scans read of a Rust source, as the compiler reads it: its words, its marks and its
/// string literals, past comments, escaped quotes, raw strings and character literals (`'"'`),
/// which a split on quotes misreads. Each with where it begins (in characters).
#[derive(Debug, PartialEq)]
enum Token {
    Word(String),
    Mark(char),
    Text(String),
}

fn tokens(text: &str) -> Vec<(usize, Token)> {
    let chars: Vec<char> = text.chars().collect();
    let mut found = Vec::new();
    let mut at = 0;
    while at < chars.len() {
        let here = chars[at];
        let next = chars.get(at + 1).copied();
        let hashes = chars[at + 1..].iter().take_while(|c| **c == '#').count();
        if here.is_whitespace() {
            at += 1;
        } else if here == '/' && next == Some('/') {
            while at < chars.len() && chars[at] != '\n' {
                at += 1;
            }
        } else if here == '/' && next == Some('*') {
            at += 2;
            while at + 1 < chars.len() && !(chars[at] == '*' && chars[at + 1] == '/') {
                at += 1;
            }
            at += 2;
        } else if here == 'r' && chars.get(at + 1 + hashes) == Some(&'"') {
            let open = at + 1 + hashes;
            let mut end = open + 1;
            let mut text = String::new();
            while end < chars.len() {
                if chars[end] == '"' && chars[end + 1..].iter().take(hashes).all(|c| *c == '#') {
                    break;
                }
                text.push(chars[end]);
                end += 1;
            }
            found.push((at, Token::Text(text)));
            at = end + 1 + hashes;
        } else if here.is_alphanumeric() || here == '_' {
            let mut end = at;
            while end < chars.len() && (chars[end].is_alphanumeric() || chars[end] == '_') {
                end += 1;
            }
            found.push((at, Token::Word(chars[at..end].iter().collect())));
            at = end;
        } else if here == '"' {
            let mut end = at + 1;
            let mut text = String::new();
            while end < chars.len() && chars[end] != '"' {
                if chars[end] == '\\' {
                    end += 1;
                }
                if let Some(c) = chars.get(end) {
                    text.push(*c);
                }
                end += 1;
            }
            found.push((at, Token::Text(text)));
            at = end + 1;
        } else if here == '\'' {
            // A character literal ('"', '\'', 'x'), else a lifetime ('a).
            if next == Some('\\') {
                let mut end = at + 2;
                while end < chars.len() && chars[end] != '\'' {
                    end += 1;
                }
                at = end + 1;
            } else if chars.get(at + 2) == Some(&'\'') {
                at += 3;
            } else {
                at += 1;
            }
        } else {
            found.push((at, Token::Mark(here)));
            at += 1;
        }
    }
    found
}

/// The string literals of a Rust source, as the compiler reads them.
fn literals(text: &str) -> Vec<String> {
    tokens(text)
        .into_iter()
        .filter_map(|(_, token)| match token {
            Token::Text(text) => Some(text),
            _ => None,
        })
        .collect()
}

/// The reader of literals reads what a split on quotes misreads.
#[test]
fn the_scans_reader_of_literals_reads_past_what_is_no_literal() {
    let source = r##"let a = "ONE"; // "NOT_IN_A_COMMENT"
let b = '"'; let c = "TWO_\"X"; /* "NOR_HERE" */ fn f<'a>(x: &'a str) {}
let d = r#"RAW_"ONE"#; let e = '\''; let g = "THREE";"##;
    assert_eq!(literals(source), ["ONE", "TWO_\"X", "RAW_\"ONE", "THREE"]);
}
