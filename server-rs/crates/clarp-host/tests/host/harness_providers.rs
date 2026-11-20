//! Signing a harness in to its providers of models from an app (`/harness-providers`):
//! OpenCode's store and catalogue under a stand-in home, OpenCode itself a stand-in that
//! keeps what it is asked. Every key and code here is made up.
use super::*;

fn catalogue() -> Value {
    json!({
        "openai": {"name": "OpenAI", "env": ["FIXTURE_ABSENT_KEY"], "models": {
            "gpt-9": {"name": "GPT 9", "variants": {"high": {}, "low": {}}},
            "deepseek-mini": {"name": "DS mini"}}},
        "groq": {"name": "Groq", "models": {"llama": {}}},
        "deepseek": {"name": "DeepSeek", "env": ["FIXTURE_PROVIDER_KEY"],
                     "models": {"deepseek-chat": {"name": "DeepSeek Chat"}}},
        "github-copilot": {"name": "GitHub Copilot", "models": {"gpt": {}}},
        "broken": {"name": "Broken", "models": {}},
        "zeta": {"name": "zeta", "models": {}},
    })
}

/// A stand-in machine: OpenCode's two stores and OpenCode's program, in the test's place.
struct Machine {
    root: PathBuf,
    store: PathBuf,
    world: Vec<(String, String)>,
}

impl Machine {
    fn new(place: &Place) -> Self {
        let root = place.root.clone();
        let cache = root.join("cache-home");
        std::fs::create_dir_all(cache.join("opencode")).unwrap();
        std::fs::write(cache.join("opencode/models.json"), catalogue().to_string()).unwrap();
        let stand_in = root.join("opencode-stand-in");
        program(
            &stand_in,
            include_str!("../fixtures/opencode-providers-stand-in.py"),
        );
        let text = |p: &PathBuf| p.to_str().unwrap().to_string();
        let world = vec![
            ("HOME".to_string(), text(&root)),
            ("XDG_DATA_HOME".to_string(), text(&root.join("data-home"))),
            ("XDG_CACHE_HOME".to_string(), text(&cache)),
            ("CLARP_OPENCODE_PROGRAM".to_string(), text(&stand_in)),
            ("FAKE_ROOT".to_string(), text(&root)),
            ("FIXTURE_PROVIDER_KEY".to_string(), "present".to_string()),
            ("FIXTURE_ABSENT_KEY".to_string(), String::new()),
        ];
        let store = root.join("data-home/opencode/auth.json");
        Self { root, store, world }
    }

    fn start(&self, place: &Place, args: &[&str], more: &[(&str, &str)]) -> Host {
        let mut world: Vec<(&str, &str)> = self
            .world
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        world.extend_from_slice(more);
        place.start_in("", args, &world)
    }

    /// What OpenCode's server was asked, in order.
    fn asked(&self) -> Vec<Value> {
        std::fs::read_to_string(self.root.join("serve-requests"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    /// How many of OpenCode's servers have ended.
    fn ended(&self) -> usize {
        std::fs::read_to_string(self.root.join("serve-ended"))
            .unwrap_or_default()
            .lines()
            .count()
    }

    fn stored(&self) -> Option<String> {
        std::fs::read_to_string(&self.store).ok()
    }
}

fn row<'a>(listing: &'a Value, id: &str) -> &'a Value {
    listing["providers"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["id"] == id)
        .unwrap()
}

fn ids(listing: &Value) -> Vec<&str> {
    listing["providers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect()
}

/// What the Host answers a change, with its body as it is sent.
fn change(host: &Host, path: &str, body: &[u8]) -> (u16, Value) {
    let got = fetch(
        host,
        "",
        "POST",
        path,
        &[("Content-Type", "application/json")],
        body,
    );
    (
        got.status,
        serde_json::from_str(&got.text()).unwrap_or(Value::Null),
    )
}

fn error(words: &str) -> Value {
    json!({ "error": words })
}

const NOT_KEPT: &str = clarp_kernel::harness_providers::NOT_KEPT;

#[test]
fn a_harness_is_signed_in_to_its_providers_only_by_the_host_that_keeps_the_tools() {
    // Beside a Host that keeps the tools, and with no mode at all: the readings answer from
    // OpenCode's files alone, OpenCode is never started (not even to ask its sign-in methods,
    // with none remembered or with what was remembered long ago), every change is refused in
    // words before anything is read, and nothing is written.
    let beside = Place::new("harness-providers-beside");
    let other = Machine::new(&beside);
    std::fs::create_dir_all(other.store.parent().unwrap()).unwrap();
    let seeded = r#"{"groq": {"type": "api", "key": "made-up-before"}}"#;
    std::fs::write(&other.store, seeded).unwrap();
    let remember = |text: Option<&str>| {
        use clarp_ports::kv::KeyValueEngine as _;
        let mut store = clarp_store_sqlite::SqliteKeyValue
            .open(&beside.root.join("data/fleet.db"))
            .unwrap();
        let key = clarp_kernel::harness_providers::METHODS_KEY;
        match text {
            Some(text) => store.put("setting", key, text.as_bytes()).unwrap(),
            None => {
                store.delete("setting", key).unwrap();
            }
        }
    };
    let long_ago = r#"{"version":"1","at":1000.0,"methods":{"openai":[{"index":1,"type":"oauth","label":"Remembered","prompts":[]}]}}"#;
    let api = json!({"index": -1, "type": "api", "label": "API key", "prompts": []});
    let remembered = json!({"index": 1, "type": "oauth", "label": "Remembered", "prompts": []});
    for (args, old) in [
        (&["--accounts", "follow"][..], false),
        (&["--accounts", "follow"][..], true),
        (&[][..], false),
        (&[][..], true),
    ] {
        if old {
            remember(Some(long_ago));
        } else if beside.root.join("data/fleet.db").exists() {
            remember(None);
        }
        let host = other.start(&beside, args, &[]);
        let methods = row(&host.get("/harness-providers"), "openai")["methods"].clone();
        let shown = if old {
            json!([api, remembered])
        } else {
            json!([api])
        };
        assert_eq!(methods, shown, "{args:?}");

        let info = host.get("/server-info");
        let features = info["capabilities"]["features"].as_array().unwrap();
        assert!(
            features.contains(&json!("harness_model_providers")),
            "{info}"
        );
        assert_eq!(info["contract"]["features"]["harness_model_providers"], 75);
        let listing = host.get("/harness-providers");
        assert_eq!(row(&listing, "groq")["credential"], "api", "{listing}");
        assert_eq!(listing["installed"], true);
        for (path, body) in [
            (
                "/harness-providers/login",
                json!({"provider": "zeta", "key": "made-up-key"}),
            ),
            (
                "/harness-providers/login",
                json!({"provider": "openai", "method": 1}),
            ),
            ("/harness-providers/login", json!(["not", "an", "object"])),
            (
                "/harness-providers/login-code",
                json!({"provider": "openai", "code": "good-code"}),
            ),
            ("/harness-providers/logout", json!({"provider": "groq"})),
        ] {
            let got = change(&host, path, body.to_string().as_bytes());
            assert_eq!(got, (409, error(NOT_KEPT)), "{args:?} {path} {body}");
        }
        assert_eq!(change(&host, "/harness-providers/renew", b"{}").0, 404);
        assert_eq!(other.stored().as_deref(), Some(seeded), "{args:?}");
        let posted: Vec<Value> = other
            .asked()
            .into_iter()
            .filter(|r| r["method"] == "POST")
            .collect();
        assert!(posted.is_empty(), "{posted:?}");
        let models = host.get("/harness-providers/models?provider=groq");
        assert_eq!(models["models"][0]["id"], "groq/llama");
        host.get("/harness-providers");
        std::thread::sleep(Duration::from_millis(800));
        let started = std::fs::read_to_string(other.root.join("started")).unwrap_or_default();
        assert_eq!(started, "", "{args:?} {old}: OpenCode was started");
    }

    // A Host alone on its machine keeps the tools. Its first key makes OpenCode's folder.
    let place = Place::new("harness-providers");
    let machine = Machine::new(&place);
    let host = machine.start(&place, &["--accounts", "select"], &[]);

    // The first listing asks OpenCode which sign-ins a phone can complete, once, aside; what
    // waits for a browser on this machine is left out, the rest keep their place in its list.
    let listing = host.get("/harness-providers");
    assert_eq!(
        (
            &listing["harness"],
            &listing["store_harness"],
            &listing["installed"],
            &listing["catalogue_available"]
        ),
        (
            &json!("opencode"),
            &json!("opencode"),
            &json!(true),
            &json!(true)
        )
    );
    assert_eq!(
        row(&listing, "openai")["methods"].as_array().unwrap().len(),
        1,
        "nothing remembered yet"
    );
    let headless = json!({"index": 1, "type": "oauth", "label": "ChatGPT Pro/Plus (headless)",
                          "prompts": [{"type": "text", "key": "org"}]});
    wait_for("the sign-in methods", || {
        row(&host.get("/harness-providers"), "openai")["methods"]
            == json!([{"index": -1, "type": "api", "label": "API key", "prompts": []}, headless])
    });
    let listing = host.get("/harness-providers");
    assert_eq!(
        row(&listing, "github-copilot")["methods"][1],
        json!({"index": 0, "type": "oauth", "label": "Login with GitHub Copilot", "prompts": []})
    );
    let probe = machine.asked();
    assert_eq!(probe.len(), 1, "{probe:?}");
    assert_eq!(
        (&probe[0]["method"], &probe[0]["path"]),
        (&json!("GET"), &json!("/provider/auth"))
    );
    assert_eq!(probe[0]["authorized"], true, "the server's own password");
    let directory = machine.root.to_str().unwrap().replace('/', "%2F");
    assert_eq!(probe[0]["query"], format!("directory={directory}"));
    wait_for("the probe's server to end", || machine.ended() == 1);
    host.get("/harness-providers");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(
        machine.asked().len(),
        1,
        "what was remembered is not asked again"
    );

    // Signed in first, then the popular ones in their order, then by name; DeepSeek's key is
    // the Host's environment's. DeepSeek's card lists who serves DeepSeek, or is signed in.
    assert_eq!(
        ids(&listing),
        [
            "deepseek",
            "openai",
            "groq",
            "github-copilot",
            "broken",
            "zeta"
        ]
    );
    let env = row(&listing, "deepseek");
    assert_eq!(
        (
            &env["signed_in"],
            &env["credential"],
            &env["can_logout"],
            &env["env"]
        ),
        (
            &json!(true),
            &json!("env"),
            &json!(false),
            &json!(["FIXTURE_PROVIDER_KEY"])
        )
    );
    let openai = row(&listing, "openai");
    assert_eq!(
        (
            &openai["name"],
            &openai["popular"],
            &openai["model_count"],
            &openai["credential"]
        ),
        (&json!("OpenAI"), &json!(true), &json!(2), &Value::Null)
    );
    assert_eq!(
        ids(&host.get("/harness-providers?harness=DeepSeek")),
        ["deepseek", "openai"]
    );
    let (status, said) = host.ask("GET", "/harness-providers?harness=claude", None);
    assert_eq!(
        (status, said),
        (400, error("claude has no model providers"))
    );

    // A provider's models, from the catalogue alone.
    assert_eq!(
        host.get("/harness-providers/models?provider=openai"),
        json!({"harness": "opencode", "provider": "openai", "name": "OpenAI", "models": [
            {"id": "openai/deepseek-mini", "label": "DS mini", "model_provider": "openai", "supported_efforts": null},
            {"id": "openai/gpt-9", "label": "GPT 9", "model_provider": "openai", "supported_efforts": ["low", "high"]}]})
    );
    let deepseek = host.get("/harness-providers/models?harness=deepseek&provider=openai");
    assert_eq!(deepseek["models"].as_array().unwrap().len(), 1);
    let (status, said) = host.ask("GET", "/harness-providers/models?provider=nothing", None);
    assert_eq!((status, said), (400, error("unknown provider nothing")));

    // A key is written to OpenCode's store as the reference writes it, in one move, 0600 in a
    // folder made 0700, and told to nobody. The app's picker learns of it at once.
    let picker =
        || host.get("/agent-model-options")["providers"]["opencode"]["model_providers"].clone();
    let before = picker();
    assert!(
        !before.as_array().unwrap().iter().any(|p| p["id"] == "zeta"),
        "{before}"
    );
    let key = "made-up-zeta-key-0123456789";
    for (provider, secret) in [("groq", "made-up-groq-key"), ("zeta", key)] {
        let body =
            json!({"harness": "opencode", "provider": provider, "key": format!(" {secret} ")});
        let got = fetch(
            &host,
            "",
            "POST",
            "/harness-providers/login",
            &[],
            body.to_string().as_bytes(),
        );
        assert_eq!(got.status, 200, "{}", got.text());
        assert!(!got.text().contains(secret), "a key is never answered");
        let said: Value = serde_json::from_str(&got.text()).unwrap();
        assert_eq!(said["ok"], true);
        assert_eq!(
            (
                &said["provider"]["id"],
                &said["provider"]["credential"],
                &said["provider"]["can_logout"]
            ),
            (&json!(provider), &json!("api"), &json!(true))
        );
    }
    let written = "{\n  \"groq\": {\n    \"type\": \"api\",\n    \"key\": \"made-up-groq-key\"\n  },\n  \"zeta\": {\n    \"type\": \"api\",\n    \"key\": \"made-up-zeta-key-0123456789\"\n  }\n}\n";
    assert_eq!(machine.stored().as_deref(), Some(written));
    use std::os::unix::fs::PermissionsExt as _;
    let mode = |p: &std::path::Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
    assert_eq!(
        (mode(&machine.store), mode(machine.store.parent().unwrap())),
        (0o600, 0o700)
    );
    let after = picker();
    assert!(
        after
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p["id"] == "zeta" && p["signed_in"] == true),
        "{after}"
    );
    assert!(
        !host.heard.lock().unwrap().contains(key),
        "a key is never logged"
    );

    // What is refused writes nothing.
    for (body, status, words) in [
        (&b"[1]"[..], 400, "bad json"),
        (
            br#"{"provider": "zeta", "key": "a\nb"}"#,
            400,
            "API key is not in the expected format",
        ),
        (
            br#"{"provider": "zeta", "key": "   "}"#,
            400,
            "API key is required",
        ),
        (
            br#"{"provider": "Bad Id", "key": "k"}"#,
            400,
            "provider is required",
        ),
        (
            br#"{"harness": "claude", "provider": "zeta", "key": "k"}"#,
            400,
            "claude has no model providers",
        ),
        (
            br#"{"harness": 5, "provider": "zeta", "key": "k"}"#,
            400,
            "5 has no model providers",
        ),
    ] {
        assert_eq!(
            change(&host, "/harness-providers/login", body),
            (status, error(words))
        );
        assert_eq!(machine.stored().as_deref(), Some(written));
    }
    // A body of nothing is an object of nothing, as there: it names no provider.
    assert_eq!(
        change(&host, "/harness-providers/login", b""),
        (400, error("provider is required"))
    );
    std::fs::write(&machine.store, "not json").unwrap();
    let save = br#"{"provider": "zeta", "key": "made-up"}"#;
    let unreadable = "OpenCode's credential store could not be read";
    assert_eq!(
        change(&host, "/harness-providers/login", save),
        (409, error(unreadable))
    );
    assert_eq!(
        change(
            &host,
            "/harness-providers/logout",
            br#"{"provider": "zeta"}"#
        ),
        (409, error(unreadable))
    );
    assert_eq!(machine.stored().as_deref(), Some("not json"));
    std::fs::write(&machine.store, written).unwrap();
    let folder = machine.store.parent().unwrap();
    std::fs::set_permissions(folder, std::fs::Permissions::from_mode(0o500)).unwrap();
    let unwritable = "OpenCode's credential store could not be written";
    assert_eq!(
        change(&host, "/harness-providers/login", save),
        (500, error(unwritable))
    );
    std::fs::set_permissions(folder, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        std::fs::read_dir(folder).unwrap().count(),
        1,
        "nothing is left beside the store"
    );

    // Signing out takes the provider's credential away; a key of the environment is not ours.
    let (status, said) = change(
        &host,
        "/harness-providers/logout",
        br#"{"provider": " ZETA "}"#,
    );
    assert_eq!(
        (status, &said["ok"], &said["provider"]["signed_in"]),
        (200, &json!(true), &json!(false))
    );
    let groq_only =
        "{\n  \"groq\": {\n    \"type\": \"api\",\n    \"key\": \"made-up-groq-key\"\n  }\n}\n";
    assert_eq!(machine.stored().as_deref(), Some(groq_only));
    for (provider, words) in [
        ("zeta", "zeta is not signed in"),
        (
            "deepseek",
            "DeepSeek is signed in through the Host's environment",
        ),
    ] {
        let body = json!({"provider": provider}).to_string();
        assert_eq!(
            change(&host, "/harness-providers/logout", body.as_bytes()),
            (409, error(words))
        );
    }

    // A sign-in that asks for a code: the provider's page and its code are told; a second ask
    // is given the same sign-in; a wrong code ends it, its server ended first.
    let start =
        json!({"provider": "openai", "method": 1, "inputs": {"org": "made-up-org"}}).to_string();
    let (status, task) = change(&host, "/harness-providers/login", start.as_bytes());
    assert_eq!(status, 202, "{task}");
    let started_at = task["started_at"].as_i64().unwrap();
    assert_eq!(
        task,
        json!({"provider": "openai", "status": "running", "method": "ChatGPT Pro/Plus (headless)",
               "verification_url": "https://example.invalid/code",
               "instructions": "Open the page, then type ABCD-EFGH", "user_code": "ABCD-EFGH",
               "awaiting_code": true, "code_submitted": false, "started_at": started_at,
               "expires_at": started_at + 600_000, "error": ""})
    );
    let authorize = machine.asked().last().unwrap().clone();
    assert_eq!(
        (
            &authorize["path"],
            &authorize["body"],
            &authorize["authorized"]
        ),
        (
            &json!("/provider/openai/oauth/authorize"),
            &json!({"method": 1, "inputs": {"org": "made-up-org"}}),
            &json!(true)
        )
    );
    assert_eq!(host.get("/harness-providers")["tasks"]["openai"], task);
    let asked_before = machine.asked().len();
    assert_eq!(
        change(&host, "/harness-providers/login", start.as_bytes()),
        (202, task.clone())
    );
    assert_eq!(
        machine.asked().len(),
        asked_before,
        "no second sign-in is started"
    );
    let ended = machine.ended();
    let code = |code: &str| {
        let body = json!({"harness": "opencode", "provider": "openai", "code": code}).to_string();
        change(&host, "/harness-providers/login-code", body.as_bytes())
    };
    let (status, failed) = code(" bad-code ");
    assert_eq!(
        (
            status,
            &failed["status"],
            &failed["error"],
            &failed["code_submitted"],
            &failed["expires_at"]
        ),
        (
            200,
            &json!("failed"),
            &json!("The code was not accepted"),
            &json!(false),
            &json!(0)
        )
    );
    assert_eq!(
        machine.ended(),
        ended + 1,
        "the server is ended before the end is told"
    );
    assert_eq!(
        machine.asked().last().unwrap()["body"],
        json!({"method": 1, "code": "bad-code"})
    );
    assert_eq!(
        code("good-code"),
        (409, error("No sign-in is waiting for a code"))
    );
    // A code that is taken is told while OpenCode answers it; the picker it had before the
    // sign-in completes is asked again after.
    let signed = |p: &Value| p["id"] == "openai" && p["signed_in"] == true;
    assert!(!picker().as_array().unwrap().iter().any(signed));
    let (status, _) = change(&host, "/harness-providers/login", start.as_bytes());
    assert_eq!(status, 202);
    let (status, done) = std::thread::scope(|scope| {
        let given = scope.spawn(|| code("slow-code"));
        wait_for("the code to be told as given", || {
            let task = &host.get("/harness-providers")["tasks"]["openai"];
            task["code_submitted"] == true && task["awaiting_code"] == false
        });
        std::fs::write(machine.root.join("release-code"), "").unwrap();
        given.join().unwrap()
    });
    assert_eq!(
        (status, &done["status"], &done["error"]),
        (200, &json!("complete"), &json!(""))
    );
    let listing = host.get("/harness-providers");
    assert_eq!(
        row(&listing, "openai")["credential"],
        "oauth",
        "OpenCode wrote its store"
    );
    assert_eq!(listing["tasks"]["openai"], done);
    assert!(picker().as_array().unwrap().iter().any(signed));
    for (body, words) in [
        (
            json!({"provider": "openai", "method": -1}),
            "method must be a sign-in method index",
        ),
        (
            json!({"provider": "openai", "method": true}),
            "method must be a sign-in method index",
        ),
        (
            json!({"provider": "openai", "method": 7}),
            "OpenAI has no such sign-in method",
        ),
        (
            json!({"provider": "openai", "method": 1, "inputs": {"org": 5}}),
            "inputs must map names to short strings",
        ),
        (
            json!({"provider": "openai", "code": "  "}),
            "Authorization code is required",
        ),
    ] {
        let path = if body.get("code").is_some() {
            "login-code"
        } else {
            "login"
        };
        let path = format!("/harness-providers/{path}");
        assert_eq!(
            change(&host, &path, body.to_string().as_bytes()),
            (400, error(words)),
            "{body}"
        );
    }

    // A sign-in finished in the provider's own page: no code is taken, it completes when the
    // page is approved.
    let (status, waiting) = change(
        &host,
        "/harness-providers/login",
        br#"{"provider": "github-copilot", "method": 0}"#,
    );
    assert_eq!(
        (
            status,
            &waiting["status"],
            &waiting["awaiting_code"],
            &waiting["user_code"],
            &waiting["method"]
        ),
        (
            202,
            &json!("running"),
            &json!(false),
            &json!("WXYZ-1234"),
            &json!("Login with GitHub Copilot")
        )
    );
    let body = br#"{"provider": "github-copilot", "code": "c"}"#;
    assert_eq!(
        change(&host, "/harness-providers/login-code", body),
        (409, error("This sign-in is not asking for a code"))
    );
    std::fs::write(machine.root.join("approve"), "").unwrap();
    wait_for("the page's sign-in", || {
        host.get("/harness-providers")["tasks"]["github-copilot"]["status"] == "complete"
    });
    assert_eq!(
        row(&host.get("/harness-providers"), "github-copilot")["credential"],
        "oauth"
    );

    // A server that fails the start is told, without what looks like a secret.
    let (status, broken) = change(
        &host,
        "/harness-providers/login",
        br#"{"provider": "broken", "method": 0}"#,
    );
    assert_eq!((status, &broken["status"]), (202, &json!("failed")));
    let why = broken["error"].as_str().unwrap();
    assert!(
        why.starts_with("POST /provider/broken/oauth/authorize: HTTP 500 "),
        "{why}"
    );
    assert!(
        why.contains("[redacted]") && !why.contains("sk-abcdefgh"),
        "{why}"
    );
    // A key saved for a provider takes its sign-in away from what is told.
    assert_eq!(
        host.get("/harness-providers")["tasks"]["broken"]["status"],
        "failed"
    );
    let key = br#"{"provider": "broken", "key": "made-up-broken-key"}"#;
    assert_eq!(change(&host, "/harness-providers/login", key).0, 200);
    assert!(
        host.get("/harness-providers")["tasks"]
            .get("broken")
            .is_none()
    );
    // Every server that was started (each was first asked one of these) has ended.
    let started = || {
        let first = |r: &Value| {
            r["path"] == "/provider/auth" || r["path"].as_str().unwrap().ends_with("/authorize")
        };
        machine.asked().iter().filter(|r| first(r)).count()
    };
    assert_eq!(
        started(),
        5,
        "the probe, two of OpenAI, GitHub's and the broken one"
    );
    wait_for("every server to end", || machine.ended() == started());
    drop(host);

    // A sign-in nobody finishes is given up when its time is out, and its server ended.
    let host = machine.start(
        &place,
        &["--accounts", "select"],
        &[("CLARP_SIGN_IN_TIMEOUT_MS", "3000")],
    );
    let ended = machine.ended();
    // A sign-in whose task a key took away takes no code, though its server still runs.
    let (status, _) = change(&host, "/harness-providers/login", start.as_bytes());
    assert_eq!(status, 202);
    let key = br#"{"provider": "openai", "key": "made-up-openai-key"}"#;
    assert_eq!(change(&host, "/harness-providers/login", key).0, 200);
    let body = br#"{"provider": "openai", "code": "good-code"}"#;
    assert_eq!(
        change(&host, "/harness-providers/login-code", body),
        (409, error("No sign-in is waiting for a code"))
    );
    // OpenCode's answer of nothing is a sign-in that waits for nothing it is told: no page,
    // no code, and it is given up in its time.
    let (status, silent) = change(
        &host,
        "/harness-providers/login",
        br#"{"provider": "silent", "method": 0}"#,
    );
    assert_eq!(
        (
            status,
            &silent["status"],
            &silent["verification_url"],
            &silent["awaiting_code"]
        ),
        (202, &json!("running"), &json!(""), &json!(false))
    );
    wait_for("the silent sign-in to time out", || {
        host.get("/harness-providers")["tasks"]["silent"]["status"] == "failed"
    });
    assert_eq!(
        host.get("/harness-providers")["tasks"]["silent"]["error"],
        "Sign-in timed out"
    );
    let (status, _) = change(&host, "/harness-providers/login", start.as_bytes());
    assert_eq!(status, 202);
    wait_for("the sign-in to time out", || {
        host.get("/harness-providers")["tasks"]["openai"]["status"] == "failed"
    });
    assert_eq!(
        host.get("/harness-providers")["tasks"]["openai"]["error"],
        "Sign-in timed out"
    );
    wait_for("their servers to end", || machine.ended() == ended + 3);
}

#[test]
fn opencode_is_asked_its_sign_in_methods_once_however_many_listings_wait_on_its_answer() {
    // OpenCode holds its answer while listings are asked, one after another and together:
    // each is answered at once with what is remembered (nothing yet), and none asks again.
    let place = Place::new("harness-providers-probe");
    let machine = Machine::new(&place);
    std::fs::write(machine.root.join("hold-probe"), "").unwrap();
    let host = machine.start(&place, &["--accounts", "select"], &[]);
    let methods = || {
        row(&host.get("/harness-providers"), "openai")["methods"]
            .as_array()
            .unwrap()
            .len()
    };
    let together = |shown: usize| {
        std::thread::scope(|scope| {
            let asking: Vec<_> = (0..8).map(|_| scope.spawn(methods)).collect();
            for asked in asking {
                assert_eq!(asked.join().unwrap(), shown);
            }
        });
    };
    assert_eq!(methods(), 1, "nothing remembered yet");
    wait_for("OpenCode to be asked", || machine.asked().len() == 1);
    for _ in 0..5 {
        assert_eq!(methods(), 1, "the listing does not wait for OpenCode");
    }
    together(1);

    // Let go: what OpenCode said is kept, and every listing asked after it sees it.
    std::fs::write(machine.root.join("release-probe"), "").unwrap();
    wait_for("the sign-in methods", || methods() == 2);
    wait_for("the probe's server to end", || machine.ended() == 1);
    together(2);
    for _ in 0..5 {
        assert_eq!(methods(), 2);
    }
    let asked = machine.asked();
    assert_eq!(asked.len(), 1, "OpenCode was asked once: {asked:?}");
    assert_eq!(asked[0]["path"], "/provider/auth");
    let started = std::fs::read_to_string(machine.root.join("started")).unwrap();
    let served = started.lines().filter(|l| l.starts_with("serve")).count();
    assert_eq!(served, 1, "one server was started: {started}");
}
