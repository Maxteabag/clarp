//! A Codex agent bound to a model provider of its own (feature `codex_own_provider`): its
//! turns run in its own Codex home and on its provider, on exec and on the chat lane; a home
//! whose `config.toml` names another provider never starts Codex; a Codex that reports another
//! provider is stopped, and the turn fails as `provider_mismatch`. An agent with cloud speech
//! off is never handed to a speech provider in the cloud. Every provider here is made up, and
//! every Codex home is a throwaway directory of the test's own.

use super::*;

/// A stand-in Codex. It notes where it ran (`CODEX_HOME`), what it was started with and its
/// pid beside itself, and reports the provider its home's file `reports` names (else the
/// agent's own, `stand-in`): in its rollout for exec, in what it answers on the app-server. A
/// Codex that reports another provider takes 20 s before it answers, unless it is stopped. On
/// the app-server, a home with the file `switch` makes the next turn report another provider
/// in the thread's settings once it has begun.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parent
home = Path(os.environ.get("CODEX_HOME", "/nowhere"))
thread = "019a0000-0000-7000-8000-0000000000a1"
reports_file = home / "reports"
reports = reports_file.read_text().strip() if reports_file.exists() else "stand-in"

def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")

if sys.argv[1] == "exec":
    sys.stdin.read()
    note("launches", "exec " + str(home))
    note("args", " ".join(sys.argv[1:]))
    note("pids", str(os.getpid()))
    day = home / "sessions" / "2026" / "11" / "24"
    day.mkdir(parents=True, exist_ok=True)
    rollout = day / ("rollout-2026-11-24T08-00-00-" + thread + ".jsonl")
    if not rollout.exists():
        meta = {"type": "session_meta", "payload": {"id": thread, "model_provider": reports}}
        rollout.write_text(json.dumps(meta) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)
    if reports != "stand-in":
        time.sleep(20)
        note("finished", "late")
    print(json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message", "text": "answered-on-" + reports}}), flush=True)
    print('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}', flush=True)
    raise SystemExit(0)

assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
note("launches", "app-server " + str(home))
note("pids", str(os.getpid()))
lane_thread = "lane-own-thread"
count = 0

def emit(value):
    print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": lane_thread, **fields}})

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if method == "initialized":
        continue
    result = {}
    after = None
    if method == "initialize":
        pass
    elif method in ("thread/start", "thread/resume"):
        note("thread-params", json.dumps({
            "method": method,
            "modelProvider": params.get("modelProvider"),
            "config_provider": params.get("config", {}).get("model_provider"),
        }))
        result = {"thread": {"id": lane_thread}, "modelProvider": reports}
    elif method == "turn/start":
        count += 1
        turn_id = "t%d" % count
        word = params["input"][0]["text"].split()[-1]
        result = {"turn": {"id": turn_id}}
        def after():
            notify("turn/started", turn={"id": turn_id})
            if (home / "switch").exists():
                notify("thread/settings/updated", threadSettings={"modelProvider": "openai"})
                time.sleep(20)
                note("finished", "late")
            notify("item/completed", turnId=turn_id, item={
                "id": "a-" + turn_id, "type": "agentMessage", "text": "lane-" + word})
            notify("turn/completed", turn={"id": turn_id, "status": "completed"})
    elif method == "thread/backgroundTerminals/clean":
        pass
    else:
        emit({"id": request["id"], "error": {"code": -32601, "message": "unexpected " + method}})
        continue
    emit({"id": request["id"], "result": result})
    if after:
        after()
"#;

/// A Codex home of its own for one agent, with a `config.toml` that names `provider` and
/// defines the agent's made-up provider `stand-in`.
fn own_home(place: &Place, name: &str, provider: &str) -> PathBuf {
    let home = place.root.join(name);
    std::fs::create_dir_all(&home).unwrap();
    std::fs::write(
        home.join("config.toml"),
        format!(
            "model_provider = \"{provider}\"\nmodel = \"made-up-model\"\n\n\
             [model_providers.stand-in]\nname = \"A stand-in\"\n\
             base_url = \"http://127.0.0.1:9/v1\"\nwire_api = \"responses\"\n"
        ),
    )
    .unwrap();
    home
}

/// Makes a Codex agent bound to the provider `stand-in` in `home`.
fn bound_agent(host: &Host, name: &str, home: &std::path::Path) -> String {
    let made = host.post(
        "/agents",
        json!({
            "name": name, "backend": "codex", "model": "a-deployment-name",
            "codex_home": home, "codex_provider": "stand-in",
        }),
    );
    made["session"]
        .as_str()
        .unwrap_or_else(|| panic!("{made}"))
        .to_string()
}

fn row_of(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .cloned()
        .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
}

/// Whether the Host told that a turn of this agent failed as a provider mismatch.
fn mismatched(heard: &Arc<Mutex<Vec<Value>>>, session: &str) -> usize {
    heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| {
            event["type"] == "agent-state"
                && event["session"] == session
                && event["detail"]["reason"] == "provider_mismatch"
        })
        .count()
}

fn gone(pid: &str) -> bool {
    !std::path::Path::new(&format!("/proc/{}", pid.trim())).exists()
}

#[test]
fn a_bound_agent_runs_in_its_own_home_on_its_own_provider_and_never_on_another() {
    let place = Place::new("codex-own-provider");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in("", &[], &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())]);
    let heard = host.listen();
    let read = |name: &str| std::fs::read_to_string(place.root.join(name)).unwrap_or_default();

    // Half a binding, a binding of another tool, and a home that is no directory are refused.
    let refused = |body: Value| {
        let (status, said) = host.ask("POST", "/agents", Some(&body));
        assert_eq!(status, 400, "{said}");
        assert_eq!(said["error"], "invalid_codex_provider", "{said}");
    };
    refused(json!({"name": "Half", "backend": "codex", "codex_provider": "stand-in"}));
    refused(json!({"name": "Other", "backend": "claude",
        "codex_home": place.root, "codex_provider": "stand-in"}));
    refused(json!({"name": "Nowhere", "backend": "codex",
        "codex_home": place.root.join("none"), "codex_provider": "stand-in"}));

    // Bound: its turn runs with its own CODEX_HOME and the provider on the command line, and
    // any model name is taken.
    let home = own_home(&place, "home-a", "stand-in");
    let ada = bound_agent(&host, "Ada", &home);
    let row = row_of(&host, &ada);
    assert_eq!(row["codex_home"], json!(home), "{row}");
    assert_eq!(row["codex_provider"], "stand-in", "{row}");
    assert_eq!(
        row["cloud_speech"], false,
        "a bound agent's words stay off the cloud: {row}"
    );
    assert_eq!(row["model"], "a-deployment-name", "{row}");
    host.send(&ada, "hello there", "u-ada-1");
    wait_for("the bound answer", || {
        host.conversation(&ada)
            .contains(&said("assistant", "answered-on-stand-in"))
    });
    assert_eq!(read("launches"), format!("exec {}\n", home.display()));
    let args = read("args");
    assert!(args.contains("-c model_provider=\"stand-in\""), "{args}");
    assert!(args.contains("--model a-deployment-name"), "{args}");

    // A home whose config names another provider never starts Codex: the turn fails as a
    // provider mismatch at once, and is not tried again.
    let wrong = own_home(&place, "home-b", "openai");
    let bea = bound_agent(&host, "Bea", &wrong);
    host.send(&bea, "hello there", "u-bea-1");
    wait_for("the refused turn", || mismatched(&heard, &bea) > 0);
    wait_for("Bea at rest", || row_of(&host, &bea)["busy"] == false);
    std::thread::sleep(Duration::from_secs(3));
    assert_eq!(mismatched(&heard, &bea), 1, "never tried again");
    assert_eq!(row_of(&host, &bea)["busy"], false);
    assert!(
        !read("launches").contains(&wrong.display().to_string()),
        "{}",
        read("launches")
    );
    let ends: Vec<Value> = host
        .turns(&bea)
        .into_iter()
        .filter(|row| row["turn"].is_object())
        .collect();
    assert_eq!(ends.len(), 1, "{ends:#?}");
    assert!(
        ends[0]["turn"]["reason"]
            .as_str()
            .is_some_and(|reason| reason.contains("another model provider")),
        "{ends:#?}"
    );

    // A Codex that reports another provider than its agent's is stopped at once, and nothing
    // it says is the agent's.
    let third = own_home(&place, "home-c", "stand-in");
    std::fs::write(third.join("reports"), "openai").unwrap();
    let cleo = bound_agent(&host, "Cleo", &third);
    host.send(&cleo, "hello there", "u-cleo-1");
    wait_for("the stopped turn", || mismatched(&heard, &cleo) > 0);
    wait_for("Cleo at rest", || row_of(&host, &cleo)["busy"] == false);
    let pid = read("pids").lines().last().unwrap().to_string();
    wait_for("the stand-in to be gone", || gone(&pid));
    assert!(!read("finished").contains("late"));
    assert!(
        !host
            .conversation(&cleo)
            .iter()
            .any(|(_, text)| text.contains("answered-on-openai")),
        "{:?}",
        host.conversation(&cleo)
    );

    // Let go of its binding, a bound agent can be bound again; a Claude agent cannot be.
    let (status, said) = host.ask(
        "POST",
        "/agent-provider",
        Some(&json!({"session": ada, "codex_provider": ""})),
    );
    assert_eq!(status, 400, "half a binding is refused: {said}");
    let cleared = host.post(
        "/agent-provider",
        json!({"session": ada, "codex_home": null, "codex_provider": null}),
    );
    assert_eq!(cleared["codex_provider"], "", "{cleared}");
    assert_eq!(cleared["cloud_speech"], true, "{cleared}");
    let bound = host.post(
        "/agent-provider",
        json!({"session": ada, "codex_home": home, "codex_provider": "stand-in",
            "cloud_speech": false}),
    );
    assert_eq!(bound["codex_provider"], "stand-in", "{bound}");
    // It stays on Codex.
    let (status, said) = host.ask(
        "POST",
        "/agent-transit",
        Some(&json!({"session": ada, "backend": "claude", "model": ""})),
    );
    assert_eq!(status, 409, "{said}");
    assert_eq!(said["error"], "own_provider", "{said}");
}

#[test]
fn a_bound_agent_on_the_chat_lane_runs_in_its_own_home_and_leaves_another_provider() {
    let place = Place::new("codex-own-lane");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        "codex_chat_lane = true\ncodex_lane_idle_minutes = 15\n",
    )
    .unwrap();
    let world = [
        ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let heard = host.listen();
    let read = |name: &str| std::fs::read_to_string(place.root.join(name)).unwrap_or_default();

    // Its lane runs in its own home, and names its provider on the thread.
    let home = own_home(&place, "lane-home", "stand-in");
    let ada = bound_agent(&host, "Ada", &home);
    host.send(&ada, "say first", "u-lane-1");
    wait_for("the lane's answer", || {
        host.conversation(&ada)
            .contains(&said("assistant", "lane-first"))
    });
    assert_eq!(read("launches"), format!("app-server {}\n", home.display()));
    let started: Value =
        serde_json::from_str(read("thread-params").lines().next().unwrap()).unwrap();
    assert_eq!(
        (&started["modelProvider"], &started["config_provider"]),
        (&json!("stand-in"), &json!("stand-in")),
        "{started}"
    );

    // Codex names another provider in the thread's settings while a turn runs: the lane ends
    // it at once, and the turn fails as a provider mismatch.
    wait_for("Ada at rest", || row_of(&host, &ada)["busy"] == false);
    std::fs::write(home.join("switch"), "").unwrap();
    host.send(&ada, "say second", "u-lane-2");
    wait_for("the stopped lane turn", || mismatched(&heard, &ada) > 0);
    wait_for("Ada at rest again", || row_of(&host, &ada)["busy"] == false);
    let pid = read("pids").lines().last().unwrap().to_string();
    wait_for("the lane's Codex to be gone", || gone(&pid));
    assert!(!read("finished").contains("late"));
    assert!(
        !host
            .conversation(&ada)
            .contains(&said("assistant", "lane-second")),
        "{:?}",
        host.conversation(&ada)
    );

    // A home whose config names another provider starts no lane.
    let wrong = own_home(&place, "lane-wrong", "openai");
    let bea = bound_agent(&host, "Bea", &wrong);
    host.send(&bea, "say third", "u-lane-3");
    wait_for("the refused lane turn", || mismatched(&heard, &bea) > 0);
    assert!(
        !read("launches").contains(&wrong.display().to_string()),
        "{}",
        read("launches")
    );
}

#[test]
fn an_agent_with_cloud_speech_off_is_never_sent_to_a_cloud_voice() {
    let place = Place::new("cloud-speech-off");
    let cartesia = cartesia_stand_in(vec![19; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n\
         model='sonic-3'\n",
    )
    .unwrap();
    let home = place.root.to_str().unwrap();
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "none",
            "--speech-keys",
            owners.to_str().unwrap(),
        ],
        &[
            ("HOME", home),
            ("XDG_CONFIG_HOME", home),
            ("XDG_DATA_HOME", home),
            ("XDG_CACHE_HOME", home),
            ("XDG_STATE_HOME", home),
            ("CLARP_CARTESIA_ENDPOINT", &cartesia.address),
            ("GOOGLE_APPLICATION_CREDENTIALS", ""),
            ("GEMINI_API_KEY", ""),
            ("DEEPGRAM_API_KEY", ""),
            ("GOOGLE_VERTEX_API_KEY", ""),
        ],
    );
    let voice = json!({"cartesia": "fixture-voice"}).to_string();
    // Each agent has a voice of its own: two agents never share one.
    let make = |name: &str, more: Value| {
        let own = json!({"cartesia": format!("fixture-voice-{name}")}).to_string();
        let mut body = json!({"name": name, "backend": "scripted", "voice_id": own});
        body.as_object_mut()
            .unwrap()
            .extend(more.as_object().unwrap().clone());
        host.post("/agents", body)["session"]
            .as_str()
            .unwrap()
            .to_string()
    };
    let quiet = make("Quiet", json!({"cloud_speech": false}));
    let spoken = make("Spoken", json!({}));
    assert_eq!(row_of(&host, &quiet)["cloud_speech"], false);
    assert!(
        row_of(&host, &spoken).get("cloud_speech").is_none(),
        "an agent that set nothing has the row it always had"
    );
    let asked = |text: &str| {
        cartesia.asked().into_iter().any(|row| {
            row["transcript"]
                .as_str()
                .is_some_and(|spoken| spoken.contains(text))
        })
    };

    host.send(&quiet, "<speak>words kept off the cloud</speak>", "u-quiet");
    host.send(&spoken, "<speak>words spoken aloud</speak>", "u-spoken");
    wait_for("the other agent spoken", || asked("words spoken aloud"));
    wait_for("the quiet agent's answer", || {
        host.conversation(&quiet)
            .iter()
            .any(|(role, text)| role == "assistant" && text.contains("words kept off"))
    });
    std::thread::sleep(Duration::from_secs(2));
    assert!(!asked("words kept off the cloud"));

    // Nor is a text played for it, and a live call for it is refused.
    let (status, said) = host.ask(
        "POST",
        "/preview",
        Some(&json!({"session": quiet, "voice_id": voice, "text": "a preview kept off"})),
    );
    assert_eq!(status, 409, "{said}");
    assert!(!asked("a preview kept off"));

    // Turned on, it is spoken as any agent.
    host.post(
        "/agent-provider",
        json!({"session": quiet, "cloud_speech": true}),
    );
    host.send(&quiet, "<speak>now spoken too</speak>", "u-quiet-2");
    wait_for("the agent spoken once allowed", || asked("now spoken too"));
}

#[test]
fn the_use_of_tools_of_a_bound_agent_is_explained_by_no_model() {
    let place = Place::new("codex-own-explained");
    let (codex, asked) = explaining_model(&place);
    let world = [("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let home = own_home(&place, "explained-home", "stand-in");
    let bound = bound_agent(&host, "Ada", &home);
    let open = host.new_agent("Mike");
    let post = |path: &str, body: Value| host.ask("POST", path, Some(&body));
    assert_eq!(
        post("/tool-explanations/settings", json!({ "enabled": true })).0,
        200
    );
    let explainer = host.get("/janitors/clarp-tool-explainer")["janitor"].clone();
    let (status, on) = post(
        "/janitors/clarp-tool-explainer/enabled",
        json!({ "expected_revision": explainer["revision"], "enabled": true }),
    );
    assert_eq!(status, 200, "{on}");
    let explain = |session: &str, command: &str| {
        let item = json!({ "id": "one", "activity": { "name": "Bash", "command": command } });
        post(
            "/tool-explanations",
            json!({ "session": session, "detail_level": 2, "items": [item] }),
        )
        .1
    };
    let questions = || std::fs::read_to_string(&asked).unwrap_or_default();

    // The bound agent's call is not handed to the explainer's model: nothing waits for it.
    let kept = explain(&bound, "frobnicate kept-out.cfg");
    assert_ne!(kept["items"][0]["status"], "pending", "{kept}");
    // Another agent's is, and only that is ever asked.
    wait_for("the other agent's explanation", || {
        explain(&open, "frobnicate asked.cfg")["items"][0]["status"] == "ready"
    });
    let asked_of = questions();
    assert!(asked_of.contains("asked.cfg"), "{asked_of}");
    assert!(!asked_of.contains("kept-out.cfg"), "{asked_of}");
    let again = explain(&bound, "frobnicate kept-out.cfg");
    assert_ne!(again["items"][0]["status"], "pending", "{again}");
    assert!(!questions().contains("kept-out.cfg"));
}

#[test]
fn what_is_said_to_an_agent_with_cloud_speech_off_is_heard_by_the_hosts_own_engine_only() {
    let place = Place::new("cloud-ears-off");
    let worker = place.root.join("whisper-stand-in.sh");
    std::fs::write(&worker, WHISPER_STAND_IN).unwrap();
    let token = "speech-token";
    let no_key = [("ELEVEN_API_KEY", ""), ("DEEPGRAM_API_KEY", "")];
    let hear = |host: &Host, session: &str| -> (u16, Value) {
        let headers = [
            ("Content-Type", "audio/mp4"),
            ("X-Transcription-Session", session),
        ];
        let answer = fetch(
            host,
            token,
            "POST",
            "/transcribe",
            &headers,
            b"a clip of speech",
        );
        let said = serde_json::from_slice(&answer.body).unwrap_or(Value::Null);
        (answer.status, said)
    };
    let quiet_on = |host: &Host| {
        let made = host.post(
            "/agents",
            json!({"name": "Quiet", "backend": "scripted", "cloud_speech": false}),
        );
        made["session"].as_str().unwrap().to_string()
    };

    // With an engine of its own, the Host hears it there.
    let engine = [
        "--whisper-python",
        "sh",
        "--whisper-worker",
        worker.to_str().unwrap(),
    ];
    let host = place.start_in(token, &engine, &no_key);
    let quiet = quiet_on(&host);
    wait_for("the engine to have loaded its model", || {
        hear(&host, &quiet).0 == 200
    });
    let (status, said) = hear(&host, &quiet);
    assert_eq!(status, 200, "{said}");
    assert!(
        said["text"].as_str().unwrap_or("").contains("Open the log"),
        "{said}"
    );
    drop(host);

    // With none, it is refused in plain words, and sent nowhere.
    let bare = Place::new("cloud-ears-off-bare");
    let host = bare.start_in(token, &[], &no_key);
    let quiet = quiet_on(&host);
    let (status, said) = hear(&host, &quiet);
    assert_eq!(status, 409, "{said}");
    assert!(
        said["error"]
            .as_str()
            .is_some_and(|why| why.contains("stays off cloud speech")),
        "{said}"
    );
}
