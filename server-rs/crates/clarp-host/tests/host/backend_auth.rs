//! Signing a tool in and out from an app (`backend_auth`), with stand-ins for the tools in a
//! home of the test's own. One Host keeps a machine's tools: the one started with
//! `--accounts select` signs them in and out as the Python Host does; one beside it
//! (`follow`), or with no mode, refuses every change and starts nothing.
use super::*;

const NOT_KEPT: &str =
    "This Host does not keep this machine's tools; sign the tool in or out on the Host that does.";

/// Stand-ins for Claude Code, Codex and Grok that do what each does to sign in and out, in the
/// home `home`, and write down every command they were given in `<home>/asked`.
fn stand_in_tools(place: &Place) -> (PathBuf, Vec<(&'static str, String)>) {
    use std::os::unix::fs::PermissionsExt as _;
    let home = place.root.join("home");
    std::fs::create_dir_all(home.join(".claude")).unwrap();
    std::fs::create_dir_all(home.join(".codex")).unwrap();
    let tools = place.root.join("tools");
    std::fs::create_dir_all(&tools).unwrap();
    let start = r#"#!/bin/sh
echo "$(basename "$0") $*" >> "$HOME/asked"
"#;
    let claude = r#"
case "$1 $2" in
"auth login")
  printf 'Opening browser to sign in\n'
  printf "If the browser didn't open, visit: \033[4mhttps://claude.example/oauth/authorize?state=s1\033[0m.\n"
  printf 'Paste code here if prompted > '
  read code
  echo "code $code" >> "$HOME/asked"
  if [ "$code" = "good-code" ]; then
    printf '{"claudeAiOauth": {"accessToken": "made-up", "expiresAt": 4102444800000}}' > "$HOME/.claude/.credentials.json"
    echo 'Login successful.'
    exit 0
  fi
  echo 'OAuth error: invalid code'
  exit 1;;
"auth logout")
  if [ -e "$HOME/logout-fails" ]; then echo 'Error: logout failed sk-abcdefghijklmnop' >&2; exit 1; fi
  rm -f "$HOME/.claude/.credentials.json"
  echo 'Successfully logged out'
  exit 0;;
esac
exit 2
"#;
    let codex = r#"
case "$1" in
login)
  printf 'Follow these steps to sign in:\n\n1. Open \033[94mhttps://auth.example/codex/device\033[0m\n'
  printf '2. Enter this one-time code \033[94mABCD-EFGH1\033[0m\n' >&2
  exec sleep 120;;
esac
exit 2
"#;
    let grok = r#"
case "$1" in
models) echo 'You are not authenticated.'; exit 0;;
login) echo 'Visit https://accounts.example/device and enter WXYZ-12345'; echo 'Error: device code expired' >&2; exit 1;;
esac
exit 2
"#;
    let mut world = Vec::new();
    for (name, variable, body) in [
        ("claude", "CLARP_CLAUDE_PROGRAM", claude),
        ("codex", "CLARP_CODEX_PROGRAM", codex),
        ("grok", "CLARP_GROK_PROGRAM", grok),
    ] {
        let path = tools.join(name);
        std::fs::write(&path, format!("{start}{body}")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        world.push((variable, path.to_str().unwrap().to_string()));
    }
    world.push(("HOME", home.to_str().unwrap().to_string()));
    world.extend(no_other_tools(place));
    (home, world)
}

/// What of the machine's environment a Host of these tests is not given: where a tool keeps its
/// login and OpenCode its files, when the machine says so. A test Host sees no home and no
/// login of the machine's: only the stand-in home of its test (20 Nov 2026: found in these
/// tests, the Host followed these where the machine running them set them).
const NOT_THE_MACHINES: [&str; 5] = [
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
];

/// The tools a Host of these tests finds without a stand-in (it asks every tool it knows of
/// for its models): none, so that no real tool runs and no provider is reached. OpenCode, and
/// DeepSeek through it, are absent in every test Host already.
fn no_other_tools(place: &Place) -> Vec<(&'static str, String)> {
    ["CLARP_AGY_PROGRAM", "CLARP_MUSE_PROGRAM"]
        .into_iter()
        .map(|variable| {
            let absent = place.root.join(format!("no-{}", variable.to_lowercase()));
            (variable, absent.to_str().unwrap().to_string())
        })
        .collect()
}

fn row(told: &Value, tool: &str) -> Value {
    let rows = told["backends"].as_array().unwrap();
    rows.iter().find(|row| row["id"] == tool).unwrap().clone()
}

fn asked(home: &std::path::Path) -> String {
    std::fs::read_to_string(home.join("asked")).unwrap_or_default()
}

#[test]
fn the_host_that_keeps_the_tools_signs_one_in_and_out_from_an_app() {
    let place = Place::new("backend-auth-select");
    let (home, mut world) = stand_in_tools(&place);
    world.push(("CLARP_SIGN_IN_MOST_MS", "6000".to_string()));
    let world: Vec<(&str, &str)> = world.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let host = place.start_without("", &["--accounts", "select"], &world, &NOT_THE_MACHINES);
    let features = host.get("/server-info")["contract"]["features"].clone();
    assert_eq!(features["backend_auth"], 1, "{features}");
    let tasks = |host: &Host| host.get("/backend-auth?harnesses=all")["tasks"].clone();
    let options = host.get("/agent-model-options");
    for tool in ["claude", "codex", "grok"] {
        assert_eq!(options["providers"][tool]["supports_auth"], true, "{tool}");
    }
    assert_eq!(options["providers"]["agy"]["supports_auth"], false);

    // Before: nobody is signed in, and every tool with a sign-in of its own can be.
    let before = host.get("/backend-auth?harnesses=all");
    let claude = row(&before, "claude");
    assert_eq!(
        (&claude["can_login"], &claude["note"], &claude["can_logout"]),
        (&json!(true), &json!(""), &json!(false))
    );
    assert_eq!(row(&before, "grok")["can_login"], true);
    assert_eq!(row(&before, "agy")["can_login"], false);
    assert_eq!(before["tasks"]["claude"]["status"], "idle");
    let plain = host.get("/backend-auth");
    assert!(plain["tasks"].get("grok").is_none(), "{plain}");
    assert_eq!(plain["tasks"]["codex"]["status"], "idle");
    assert!(row(&plain, "claude").get("can_login").is_none());

    // A sign-in starts the tool's own and is answered 202 with it running.
    let (status, started) = host.ask(
        "POST",
        "/backend-auth/login",
        Some(&json!({ "backend": " Claude " })),
    );
    assert_eq!(status, 202, "{started}");
    assert_eq!(
        (&started["backend"], &started["status"]),
        (&json!("claude"), &json!("running"))
    );
    wait_for("the prompt for a code", || {
        tasks(&host)["claude"]["awaiting_code"] == true
    });
    let waiting = tasks(&host)["claude"].clone();
    assert_eq!(
        waiting["verification_url"],
        "https://claude.example/oauth/authorize?state=s1"
    );
    assert!(
        !waiting["output"].as_str().unwrap().contains('\u{1b}'),
        "{waiting}"
    );
    assert_eq!(
        waiting["expires_at"],
        waiting["started_at"].as_i64().unwrap() + 600_000
    );
    assert_eq!(waiting["code_submitted"], false);
    // Asked again while it runs: the same sign-in, no second tool.
    let (status, again) = host.ask(
        "POST",
        "/backend-auth/login",
        Some(&json!({ "backend": "claude" })),
    );
    assert_eq!(
        (status, &again["started_at"]),
        (202, &waiting["started_at"])
    );

    // A code is looked at before the sign-in, and refused in the reference's words.
    let code = |tool: &str, code: Value| {
        host.ask(
            "POST",
            "/backend-auth/login-code",
            Some(&json!({ "backend": tool, "code": code })),
        )
    };
    for (tool, said, status, why) in [
        ("claude", json!(""), 400, "Authorization code is required"),
        (
            "claude",
            json!("   "),
            400,
            "Authorization code is required",
        ),
        ("claude", json!(null), 400, "Authorization code is required"),
        (
            "claude",
            json!("a\nb"),
            400,
            "Authorization code is not in the expected format",
        ),
        (
            "claude",
            json!("x".repeat(513)),
            400,
            "Authorization code is not in the expected format",
        ),
        ("agy", json!("c"), 400, "agy CLI is not installed"),
        ("", json!("c"), 400, " CLI is not installed"),
        ("codex", json!("c"), 409, "No sign-in is waiting for a code"),
    ] {
        let (answered, body) = code(tool, said.clone());
        assert_eq!(
            (answered, body["error"].as_str()),
            (status, Some(why)),
            "{tool} {said}"
        );
    }
    // The code goes to the tool, without its blanks; the prompt it answered asks no more.
    let (status, sent) = code("claude", json!("  good-code "));
    assert_eq!(status, 200, "{sent}");
    assert_eq!(
        (&sent["code_submitted"], &sent["awaiting_code"]),
        (&json!(true), &json!(false))
    );
    wait_for("the sign-in to complete", || {
        tasks(&host)["claude"]["status"] == "complete"
    });
    assert_eq!(tasks(&host)["claude"]["error"], "");
    let record = asked(&home);
    let started = record.find("claude auth login --claudeai\n");
    let given = record.find("code good-code\n");
    assert!(started.is_some() && started < given, "{record}");
    let signed = row(&host.get("/backend-auth"), "claude");
    assert_eq!(
        (
            &signed["credential_present"],
            &signed["can_logout"],
            &signed["state"]
        ),
        (&json!(true), &json!(true), &json!("unverified"))
    );
    let (status, late) = code("claude", json!("good-code"));
    assert_eq!(
        (status, late["error"].as_str()),
        (409, Some("No sign-in is waiting for a code"))
    );

    // A wrong code: the tool fails, and the app is told the line it failed with.
    host.ask(
        "POST",
        "/backend-auth/login",
        Some(&json!({ "backend": "claude" })),
    );
    wait_for("the second prompt", || {
        tasks(&host)["claude"]["awaiting_code"] == true
    });
    assert_eq!(code("claude", json!("bad")).0, 200);
    wait_for("the sign-in to fail", || {
        tasks(&host)["claude"]["status"] == "failed"
    });
    assert_eq!(tasks(&host)["claude"]["error"], "OAuth error: invalid code");

    // A sign-out the tool refuses is refused with its reason, a key in it hidden.
    std::fs::write(home.join("logout-fails"), "").unwrap();
    let (status, refused) = host.ask(
        "POST",
        "/backend-auth/logout",
        Some(&json!({ "backend": "claude" })),
    );
    assert_eq!(
        (status, refused["error"].as_str()),
        (409, Some("Error: logout failed [redacted]"))
    );
    assert!(home.join(".claude/.credentials.json").exists());
    std::fs::remove_file(home.join("logout-fails")).unwrap();
    let (status, out) = host.ask(
        "POST",
        "/backend-auth/logout",
        Some(&json!({ "backend": "claude" })),
    );
    assert_eq!(status, 200, "{out}");
    assert_eq!(
        (
            &out["id"],
            &out["state"],
            &out["can_logout"],
            &out["can_login"]
        ),
        (
            &json!("claude"),
            &json!("signed_out"),
            &json!(false),
            &json!(true)
        )
    );
    assert!(!home.join(".claude/.credentials.json").exists());
    assert_eq!(
        tasks(&host)["claude"]["status"],
        "idle",
        "a sign-out forgets the sign-in"
    );

    // A device's sign-in: its address and code, from its output and its errors alike, and
    // the tool stopped when it runs too long.
    let (status, _) = host.ask(
        "POST",
        "/backend-auth/login",
        Some(&json!({ "backend": "codex" })),
    );
    assert_eq!(status, 202);
    wait_for("the device code", || {
        tasks(&host)["codex"]["user_code"] == "ABCD-EFGH1"
    });
    let device = tasks(&host)["codex"].clone();
    assert_eq!(
        device["verification_url"],
        "https://auth.example/codex/device"
    );
    assert_eq!(device["awaiting_code"], false);
    let (status, unasked) = code("codex", json!("c"));
    assert_eq!(
        (status, unasked["error"].as_str()),
        (409, Some("This sign-in is not asking for a code"))
    );
    wait_for("the sign-in to time out", || {
        tasks(&host)["codex"]["status"] == "failed"
    });
    assert_eq!(tasks(&host)["codex"]["error"], "Login timed out.");
    let (status, _) = host.ask(
        "POST",
        "/backend-auth/login",
        Some(&json!({ "backend": "grok" })),
    );
    assert_eq!(status, 202);
    wait_for("Grok to fail", || {
        tasks(&host)["grok"]["status"] == "failed"
    });
    let grok = tasks(&host)["grok"].clone();
    assert_eq!(
        (&grok["error"], &grok["user_code"]),
        (&json!("Error: device code expired"), &json!("WXYZ-12345"))
    );

    // Tools that are not signed in from here, or not on this machine, and what is no request.
    std::fs::remove_file(place.root.join("tools/codex")).unwrap();
    for (path, body, status, why) in [
        (
            "/backend-auth/login",
            json!({ "backend": "opencode" }),
            400,
            "opencode CLI is not installed",
        ),
        (
            "/backend-auth/login",
            json!({ "backend": "codex" }),
            400,
            "codex CLI is not installed",
        ),
        (
            "/backend-auth/logout",
            json!({ "backend": "codex" }),
            400,
            "codex CLI is not installed",
        ),
        (
            "/backend-auth/logout",
            json!({}),
            400,
            " CLI is not installed",
        ),
    ] {
        let (answered, said) = host.ask("POST", path, Some(&body));
        assert_eq!(
            (answered, said["error"].as_str()),
            (status, Some(why)),
            "{path} {body}"
        );
    }
    let bad = fetch(
        &host,
        "",
        "POST",
        "/backend-auth/login",
        &[("Content-Type", "application/json")],
        b"[1]",
    );
    assert_eq!((bad.status, bad.text().contains("bad json")), (400, true));
    let none = fetch(&host, "", "POST", "/backend-auth/whoami", &[], b"{}");
    assert_eq!((none.status, none.text()), (404, "not found".to_string()));
}

#[test]
fn a_host_that_does_not_keep_the_tools_refuses_every_change_and_starts_nothing() {
    for mode in [Some("follow"), None] {
        let place = Place::new("backend-auth-follow");
        let (home, world) = stand_in_tools(&place);
        let world: Vec<(&str, &str)> = world.iter().map(|(k, v)| (*k, v.as_str())).collect();
        let more: Vec<&str> = mode.map_or(vec![], |mode| vec!["--accounts", mode]);
        let host = place.start_without("", &more, &world, &NOT_THE_MACHINES);
        let features = host.get("/server-info")["contract"]["features"].clone();
        assert!(
            features.get("backend_auth").is_none(),
            "{mode:?}: {features}"
        );
        for (path, body) in [
            ("/backend-auth/login", &br#"{"backend": "claude"}"#[..]),
            (
                "/backend-auth/login-code",
                br#"{"backend": "claude", "code": "c"}"#,
            ),
            ("/backend-auth/logout", br#"{"backend": "claude"}"#),
            ("/backend-auth/login", b"not json"),
            ("/backend-auth/logout", br#"{"backend": "nothing"}"#),
        ] {
            let got = fetch(
                &host,
                "",
                "POST",
                path,
                &[("Content-Type", "application/json")],
                body,
            );
            let said: Value = serde_json::from_str(&got.text()).unwrap();
            assert_eq!(
                (got.status, said["error"].as_str()),
                (409, Some(NOT_KEPT)),
                "{mode:?} {path}"
            );
        }
        // Anything else under the door is still no route.
        let none = fetch(&host, "", "POST", "/backend-auth/log%69n", &[], b"{}");
        assert_eq!(none.status, 404);
        // The reads are as before: no tool was started, none can be signed in from here.
        std::thread::sleep(Duration::from_millis(300));
        assert!(!asked(&home).contains("login"), "{}", asked(&home));
        assert!(!asked(&home).contains("logout"), "{}", asked(&home));
        let told = host.get("/backend-auth?harnesses=all");
        let claude = row(&told, "claude");
        assert_eq!(
            (&claude["can_login"], &claude["can_logout"], &claude["note"]),
            (
                &json!(false),
                &json!(false),
                &json!("Sign in with the tool itself on this Computer.")
            )
        );
        assert_eq!(told["tasks"]["claude"]["status"], "idle");
        let options = host.get("/agent-model-options");
        assert_eq!(options["providers"]["claude"]["supports_auth"], false);
    }
}

/// A sign-out does not wait for the tool's turns (the lead's ruling of 20 Nov 2026: it is the
/// owner's own act, and the reference takes no lock). A turn that runs when its tool is signed
/// out under it fails on the login, and this Host's account rule takes it up: with no pool of
/// accounts for the tool, the turn ends and says that the sign-in is gone; the agent answers
/// again once the tool is signed in.
#[test]
fn a_turn_whose_tool_is_signed_out_under_it_ends_on_the_login_and_holds_nothing() {
    let place = Place::new("backend-auth-turn");
    let home = place.root.join("home");
    let login = home.join(".claude/.credentials.json");
    std::fs::create_dir_all(login.parent().unwrap()).unwrap();
    std::fs::write(&login, r#"{"claudeAiOauth": {"accessToken": "made-up"}}"#).unwrap();
    // The turn told to hold runs until the login is gone; a turn without a login fails as
    // Claude Code does.
    let script = [
        "#!/bin/sh\n",
        "if [ \"$1\" = auth ]; then\n",
        "  if [ \"$2\" = logout ]; then rm -f \"$HOME/.claude/.credentials.json\"; echo 'Successfully logged out'; exit 0; fi\n",
        "  exit 2\nfi\n",
        "read -r message\n",
        "case \"$message\" in *HOLD*) touch \"$HOME/held\"; while [ -e \"$HOME/.claude/.credentials.json\" ]; do sleep 0.1; done;; esac\n",
        "if [ ! -e \"$HOME/.claude/.credentials.json\" ]; then\n",
        "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n",
        "echo 'Not logged in. Please run /login' >&2\n",
        "exit 1\nfi\n",
        STAND_IN_ANSWERS,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("HOME", home.to_str().unwrap()),
    ];
    let others = no_other_tools(&place);
    let others = others.iter().map(|(name, path)| (*name, path.as_str()));
    let world: Vec<(&str, &str)> = world.into_iter().chain(others).collect();
    let host = place.start_without("", &["--accounts", "select"], &world, &NOT_THE_MACHINES);
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    host.send(&agent, "HOLD this", "h-1");
    wait_for("the turn to run", || home.join("held").exists());
    // The sign-out is answered while the turn runs: the turn ends only after it.
    let began = Instant::now();
    let (status, out) = host.ask(
        "POST",
        "/backend-auth/logout",
        Some(&json!({ "backend": "claude" })),
    );
    assert_eq!(
        (status, &out["state"]),
        (200, &json!("signed_out")),
        "{out}"
    );
    assert!(began.elapsed() < Duration::from_secs(10));
    assert!(!login.exists());
    let busy = |host: &Host| {
        let snapshot = host.get("/agents/snapshot");
        let rows = snapshot["agents"].as_array().unwrap().clone();
        let row = rows
            .into_iter()
            .find(|row| row["session"] == agent.as_str());
        row.unwrap()["busy"] == true
    };
    wait_for("the turn to end", || !busy(&host));
    let said = host.conversation(&agent);
    assert!(
        said.iter()
            .any(|(_, text)| text.starts_with("Sign-in expired")
                && text.contains("Not logged in. Please run /login")),
        "{said:?}"
    );
    // Signed in again, the agent answers: nothing of the turn waits or holds it.
    std::fs::write(&login, r#"{"claudeAiOauth": {"accessToken": "made-up"}}"#).unwrap();
    host.send(&agent, "again", "h-2");
    wait_for("the agent to answer", || {
        host.conversation(&agent)
            .iter()
            .any(|(role, text)| role == "assistant" && text == "on it")
    });
}
