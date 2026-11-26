use super::*;

/// A stand-in for Claude Code: says its version, and answers each message with "Heard: <it>"
/// after looking at a file with a tool, holding the turn while a file `hold` is there.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import select
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
incoming = json.loads(sys.stdin.readline())
prompt = incoming["message"]["content"][0]["text"]

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": "00000000-0000-4000-8000-000000000001", **fields}), flush=True)

send("system", subtype="init", model="claude-stand-in")
if (root / "quiet-hold").exists():
    (root / "quiet-ready").touch()
    while (root / "quiet-hold").exists():
        time.sleep(0.05)
call = "toolu_" + str(time.time_ns())
send("assistant", message={"id": "msg-look", "role": "assistant", "content": [
    {"type": "tool_use", "id": call, "name": "Read", "input": {"file_path": "/work/word.txt"}}]})
send("user", message={"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": call, "content": "The word in the file is ember."}]})
def record_followup(timeout):
    if select.select([sys.stdin], [], [], timeout)[0]:
        line = sys.stdin.readline()
        if line:
            (root / "source-received-followup").write_text(line)

if (root / "hold").exists():
    (root / "source-held").touch()
    while (root / "hold").exists():
        if (root / "record-steering").exists():
            record_followup(0.05)
        else:
            time.sleep(0.05)
    if (root / "record-steering").exists():
        record_followup(0.2)
answer = "Heard: " + prompt.split("\n")[0]
send("assistant", message={"id": "msg-" + str(time.time_ns()), "role": "assistant",
                           "content": [{"type": "text", "text": answer}]})
if "Continue the parked work." in prompt:
    send("rate_limit_event", rate_limit_info={"status": "rejected", "rateLimitType": "five_hour"})
    send("result", subtype="success", is_error=True, result="out for now")
    sys.exit(1)
send("result", subtype="success", is_error=False, result=answer)
"#;

/// A stand-in for Codex that identifies answers and reads for a Transit summary.
const CODEX: &str = r#"#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.167.1")
    sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(1)
prompt = sys.stdin.read()
thread = args[args.index("resume") + 1] if "resume" in args else "00000000-0000-7000-8000-00000000c0de"
if "--output-schema" in args:
    text = json.dumps({"summary": "Summary of the older parts."})
elif "<clarp-turn-resumed>" in prompt and "Continue the parked work." in prompt:
    text = "Codex continued the parked work."
elif "<clarp-turn-resumed>" in prompt and "Continue the active work." in prompt:
    text = "Codex continued the active work."
else:
    text = "Codex heard: " + prompt.strip().splitlines()[-1]
def say(**fields):
    print(json.dumps(fields), flush=True)
say(type="thread.started", thread_id=thread)
say(type="turn.started")
say(type="item.completed", item={"id": "item_0", "type": "agent_message", "text": text})
say(type="turn.completed", usage={"input_tokens": 5, "output_tokens": 2})
"#;

struct Setting {
    place: Place,
    home: std::path::PathBuf,
    claude: std::path::PathBuf,
    codex: std::path::PathBuf,
}

impl Setting {
    fn new(name: &str) -> Self {
        let place = Place::new(name);
        let home = place.root.join("home");
        std::fs::create_dir_all(&home).unwrap();
        let claude = place.root.join("claude");
        program(&claude, CLAUDE);
        let codex = place.root.join("codex");
        program(&codex, CODEX);
        Self {
            place,
            home,
            claude,
            codex,
        }
    }

    /// A Host whose tools keep their sessions in this test's home, never the machine's.
    fn start(&self) -> Host {
        self.start_with(&[])
    }

    fn start_with(&self, args: &[&str]) -> Host {
        let claude_dir = self.home.join(".claude");
        let codex_home = self.home.join(".codex");
        std::fs::create_dir_all(&codex_home).unwrap();
        // Signed in, as far as the Host looks.
        std::fs::write(
            codex_home.join("auth.json"),
            r#"{"OPENAI_API_KEY": "made-up-key"}"#,
        )
        .unwrap();
        std::fs::create_dir_all(&claude_dir).unwrap();
        std::fs::write(
            claude_dir.join(".credentials.json"),
            r#"{"claudeAiOauth": {"accessToken": "made-up", "expiresAt": 4102444800000}}"#,
        )
        .unwrap();
        let world = [
            ("HOME", self.home.to_str().unwrap()),
            ("CLAUDE_CONFIG_DIR", claude_dir.to_str().unwrap()),
            ("CODEX_HOME", codex_home.to_str().unwrap()),
            ("CLARP_CLAUDE_PROGRAM", self.claude.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", self.codex.to_str().unwrap()),
        ];
        self.place.start_in("", args, &world)
    }

    fn rollouts(&self) -> Vec<std::path::PathBuf> {
        let mut found = Vec::new();
        let mut dirs = vec![self.home.join(".codex").join("sessions")];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(&dir).into_iter().flatten().flatten() {
                if entry.path().is_dir() {
                    dirs.push(entry.path());
                } else if entry.file_name().to_string_lossy().starts_with("rollout-") {
                    found.push(entry.path());
                }
            }
        }
        found
    }
}

fn row(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .unwrap()
        .clone()
}

fn talked(host: &Host, session: &str) {
    host.send(session, "Remember the word lantern.", "bc-1");
    wait_for("the answer", || {
        host.conversation(session)
            .iter()
            .any(|(role, text)| role == "assistant" && text.contains("lantern"))
    });
    wait_for("the agent to be done", || {
        row(host, session)["busy"] == false
    });
}

/// Begins a change and waits until it is done or failed: its last state.
fn transit(host: &Host, ask: Value) -> Value {
    let (status, began) = host.ask("POST", "/agent-transit", Some(&ask));
    assert_eq!(status, 202, "{began}");
    finished_transit(
        host,
        ask["session"].as_str().unwrap(),
        began["transit_id"].as_str().unwrap(),
    )
}

fn finished_transit(host: &Host, session: &str, id: &str) -> Value {
    let mut last = Value::Null;
    wait_for("the change to end", || {
        last = host.get(&format!("/agent-transit?session={session}&transit_id={id}"));
        matches!(last["phase"].as_str(), Some("done" | "failed"))
    });
    last
}

#[test]
fn an_agent_moves_to_codex_with_its_conversation_and_keeps_its_name() {
    let setting = Setting::new("agent-transit");
    let host = setting.start();
    let made = host.post("/agents", json!({"name": "Mover", "backend": "claude"}));
    let session = made["session"].as_str().unwrap().to_string();
    talked(&host, &session);
    let conversation = row(&host, &session)["conversation_id"].clone();
    assert!(!conversation.as_str().unwrap_or("").is_empty());
    assert!(row(&host, &session)["transit"].is_null());

    // What can be chosen: whether each tool can be moved to, and its default model.
    let options = host.get(&format!("/agent-model-options?session={session}"));
    assert_eq!(
        options["providers"]["codex"]["available"], true,
        "{options}"
    );
    assert!(options["providers"]["codex"]["unavailable_reason"].is_null());
    assert_eq!(options["providers"]["muse"]["available"], false);
    assert_eq!(
        options["providers"]["muse"]["unavailable_reason"],
        "not_supported"
    );
    let models = options["providers"]["codex"]["models"].as_array().unwrap();
    assert_eq!(
        models
            .iter()
            .filter(|model| model["default"] == true)
            .count(),
        1
    );

    let heard = host.listen();
    let ask = json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                     "request_id": "move-1"});
    let done = transit(&host, ask.clone());
    assert_eq!(done["phase"], "done", "{done}");
    assert!(done["error"].is_null());
    assert_eq!(done["backend"], "codex");
    assert_eq!(done["model"], "gpt-5.5");
    assert_eq!(done["previous_backend"], "claude");
    assert_eq!(done["carried"]["user"], 1, "{done}");
    // The message in which it looked at the file, and its answer.
    assert_eq!(done["carried"]["assistant"], 2, "{done}");
    assert_eq!(done["carried"]["omitted"], 0);
    // The same request again begins nothing new.
    let (status, again) = host.ask("POST", "/agent-transit", Some(&ask));
    assert_eq!(status, 202);
    assert_eq!(again["transit_id"], done["transit_id"]);

    // The rollout is where Codex looks and holds what was said.
    let rollouts = setting.rollouts();
    assert_eq!(rollouts.len(), 1, "{rollouts:?}");
    let written = std::fs::read_to_string(&rollouts[0]).unwrap();
    assert!(written.contains("Remember the word lantern."));
    assert!(written.contains("Heard: Remember the word lantern."));
    // With the tool the agent used, as Codex keeps a call and what it gave back.
    let items: Vec<Value> = written
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap()["payload"].clone())
        .collect();
    let call = items
        .iter()
        .find(|item| item["type"] == "function_call")
        .expect("the call is carried");
    assert_eq!(call["name"], "Read");
    let input: Value = serde_json::from_str(call["arguments"].as_str().unwrap()).unwrap();
    assert_eq!(input["file_path"], "/work/word.txt");
    let output = items
        .iter()
        .find(|item| item["type"] == "function_call_output")
        .expect("what it gave back is carried");
    assert_eq!(output["call_id"], call["call_id"]);
    assert_eq!(output["output"], "The word in the file is ember.");

    // The row has the new tool; the conversation keeps its name, its rows and the one row
    // every app shows of the change.
    let after = row(&host, &session);
    assert_eq!(after["backend"], "codex");
    assert_eq!(after["model"], "gpt-5.5");
    assert_eq!(after["conversation_id"], conversation);
    assert!(after["transit"].is_null());
    let said = host.conversation(&session);
    assert!(
        said.iter()
            .any(|(_, text)| text == "Remember the word lantern.")
    );
    assert!(
        said.iter().any(|(role, text)| role == "system"
            && text.starts_with("Now on Codex · ")
            && text.ends_with("The conversation came along.")),
        "{said:?}"
    );
    wait_for("the phases to be told", || {
        let heard = heard.lock().unwrap();
        let phases: Vec<&str> = heard
            .iter()
            .filter(|event| event["type"] == "agent-transit")
            .filter_map(|event| event["phase"].as_str())
            .collect();
        phases == ["reading", "translating", "registering", "switching", "done"]
    });

    // A Host started again has the same.
    drop(host);
    let host = setting.start();
    let again = row(&host, &session);
    assert_eq!(again["backend"], "codex");
    assert_eq!(again["conversation_id"], conversation);
}

fn queue_for_transit(host: &Host, session: &str, id: &str, text: &str) {
    let queued = host.post(
        "/send",
        json!({"session": session, "text": text, "client_msg_id": id,
               "queue_if_busy": true, "origin": "automation"}),
    );
    assert_eq!(queued["queued"], true, "{queued}");
}

#[test]
fn transit_finishes_the_current_turn_and_runs_the_waiting_messages_on_the_target() {
    let setting = Setting::new("agent-transit-mailbox");
    let host = setting.start();
    let made = host.post("/agents", json!({"name": "Switcher", "backend": "claude"}));
    let session = made["session"].as_str().unwrap();
    let hold = setting.place.root.join("hold");
    std::fs::write(&hold, "").unwrap();
    host.send(session, "Finish this on Claude.", "switch-1");
    wait_for("the turn to run", || row(&host, session)["busy"] == true);
    wait_for("source tool work before Transit", || {
        setting.place.root.join("source-held").exists()
    });
    queue_for_transit(&host, session, "switch-2", "First pending message.");
    let ask = json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                     "request_id": "switch-with-queue"});
    let (status, began) = host.ask("POST", "/agent-transit", Some(&ask));
    assert_eq!(status, 202, "{began}");
    std::fs::write(setting.place.root.join("record-steering"), "").unwrap();
    let send = host.send(session, "Second pending message.", "switch-3");
    assert_eq!(send["queued"], true, "{send}");
    assert_eq!(row(&host, session)["backend"], "claude");
    std::fs::remove_file(hold).unwrap();

    let done = finished_transit(&host, session, began["transit_id"].as_str().unwrap());
    assert_eq!(done["phase"], "done", "{done}");
    assert!(!setting.place.root.join("source-received-followup").exists());
    wait_for("the Codex queue to drain", || {
        let row = row(&host, session);
        row["backend"] == "codex" && row["busy"] == false && row["queued_turn_count"] == 0
    });
    let answers: Vec<_> = host
        .conversation(session)
        .into_iter()
        .filter_map(|(role, text)| (role == "assistant" && !text.is_empty()).then_some(text))
        .collect();
    assert_eq!(
        answers,
        [
            "Heard: Finish this on Claude.",
            "Codex heard: First pending message.",
            "Codex heard: Second pending message.",
        ]
    );
}

#[test]
fn transit_checkpoints_an_active_quiet_attempt_before_resuming_on_the_target() {
    let setting = Setting::new("agent-transit-active-quiet");
    let host = setting.start();
    let made = host.post("/agents", json!({"name": "Active", "backend": "claude"}));
    let session = made["session"].as_str().unwrap();
    let hold = setting.place.root.join("quiet-hold");
    std::fs::write(&hold, "").unwrap();
    host.send(session, "Continue the active work.", "active-1");
    wait_for("the active Claude process", || {
        setting.place.root.join("quiet-ready").exists()
    });
    queue_for_transit(&host, session, "active-2", "After active work.");
    let done = transit(
        &host,
        json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                                    "request_id": "switch-active"}),
    );
    assert_eq!(done["phase"], "done", "{done}");
    assert!(
        hold.exists(),
        "the original attempt did not finish the fixture on its own"
    );
    wait_for("the target work and queue", || {
        let a = row(&host, session);
        a["backend"] == "codex" && a["busy"] == false && a["queued_turn_count"] == 0
    });
    let words = host.conversation(session);
    assert_eq!(
        words
            .iter()
            .filter(|(role, text)| role == "user" && text == "Continue the active work.")
            .count(),
        1
    );
    let answers: Vec<_> = words
        .iter()
        .filter_map(|(role, text)| {
            (role == "assistant" && !text.is_empty()).then_some(text.as_str())
        })
        .collect();
    assert_eq!(
        answers,
        [
            "Codex continued the active work.",
            "Codex heard: After active work."
        ]
    );
}

#[test]
fn transit_resumes_account_parked_work_and_then_the_queue() {
    let setting = Setting::new("agent-transit-account-parked");
    let host = setting.start_with(&["--accounts", "follow"]);
    let made = host.post("/agents", json!({"name": "Parked", "backend": "claude"}));
    let session = made["session"].as_str().unwrap();
    host.send(session, "Continue the parked work.", "parked-1");
    wait_for("the source account wait", || {
        row(&host, session)["live_activity"]["state"] == "limited"
    });
    queue_for_transit(&host, session, "parked-2", "After the parked work.");
    let done = transit(
        &host,
        json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                                    "request_id": "switch-account-wait"}),
    );
    assert_eq!(done["phase"], "done", "{done}");
    wait_for("the target work and queue", || {
        let a = row(&host, session);
        a["backend"] == "codex" && a["busy"] == false && a["queued_turn_count"] == 0
    });
    let words = host.conversation(session);
    assert_eq!(
        words
            .iter()
            .filter(|(role, text)| role == "user" && text == "Continue the parked work.")
            .count(),
        1,
        "the original request remains one user message"
    );
    let codex_answers: Vec<_> = words
        .iter()
        .filter_map(|(role, text)| {
            (role == "assistant" && text.starts_with("Codex ")).then_some(text.as_str())
        })
        .collect();
    assert_eq!(
        codex_answers,
        [
            "Codex continued the parked work.",
            "Codex heard: After the parked work."
        ]
    );
    let rollouts = setting.rollouts();
    assert_eq!(rollouts.len(), 1);
    let carried = std::fs::read_to_string(&rollouts[0]).unwrap();
    assert!(carried.contains("The word in the file is ember."));
}

#[test]
fn a_long_conversation_moves_under_a_summary_without_using_the_source_mailbox() {
    let setting = Setting::new("agent-transit-long");
    let host = setting.start();
    let made = host.post("/agents", json!({"name": "Long", "backend": "claude"}));
    let session = made["session"].as_str().unwrap().to_string();
    // Four messages and their answers of 150,000 signs each: more than Codex is given.
    for part in 0..4 {
        let text = format!("Long part {part}: {}", "x".repeat(150_000));
        host.send(&session, &text, &format!("long-{part}"));
        wait_for("the answer", || {
            host.conversation(&session).iter().any(|(role, text)| {
                role == "assistant" && text.starts_with(&format!("Heard: Long part {part}:"))
            })
        });
        wait_for("the agent to be done", || {
            row(&host, &session)["busy"] == false
        });
    }
    let ask = json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                     "request_id": "move-long"});
    let done = transit(&host, ask);
    assert_eq!(done["phase"], "done", "{done}");
    assert_eq!(done["carried"]["summary"], "model", "{done}");
    let omitted = done["carried"]["omitted"].as_u64().unwrap();
    assert!(omitted > 0, "{done}");

    // The rollout opens with a separate reading of the older turns, then the newest part.
    let rollouts = setting.rollouts();
    assert_eq!(rollouts.len(), 1, "{rollouts:?}");
    let items: Vec<Value> = std::fs::read_to_string(&rollouts[0])
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap()["payload"].clone())
        .collect();
    let head = items[1]["content"][0]["text"].as_str().unwrap();
    assert_eq!(items[1]["role"], "user");
    assert!(
        head.starts_with("[Clarp: this conversation moved from Claude to Codex."),
        "{}",
        &head[..200.min(head.len())]
    );
    assert!(head.contains("from Clarp's record:]"));
    assert!(head.contains("Summary of the older parts."));
    let written = std::fs::read_to_string(&rollouts[0]).unwrap();
    assert!(
        written.contains("Long part 3: "),
        "the newest goes as it was"
    );
    assert!(
        !written.contains("Long part 0: "),
        "the oldest is in the summary"
    );
    assert_eq!(
        written.matches("You are about to move").count(),
        0,
        "the separate reading does not write a turn into the conversation"
    );
    assert!(
        host.conversation(&session)
            .iter()
            .any(|(role, text)| role == "system"
                && text.ends_with(&format!("its oldest {omitted} turns as a summary.")))
    );
}

#[test]
fn a_move_forgets_which_model_answered_on_the_tool_it_left() {
    let setting = Setting::new("agent-transit-answered");
    let host = setting.start();
    // Set to no model: the row shows the model that answered last.
    let made = host.post("/agents", json!({"name": "Shown", "backend": "claude"}));
    let session = made["session"].as_str().unwrap().to_string();
    talked(&host, &session);
    wait_for("the model that answered", || {
        row(&host, &session)["model"] == "claude-stand-in"
    });
    let ask = json!({"session": session, "backend": "codex", "model": "",
                     "request_id": "move-answered"});
    let done = transit(&host, ask);
    assert_eq!(done["phase"], "done", "{done}");
    // Nothing has answered on Codex yet: the row names no model, not Claude's.
    let after = row(&host, &session);
    assert_eq!(after["backend"], "codex");
    assert_eq!(after["model"], "", "{after}");
    // Nor once the Host is started again and reads the turns' ends from the log.
    drop(host);
    let host = setting.start();
    let again = row(&host, &session);
    assert_eq!(again["backend"], "codex");
    assert_eq!(again["model"], "", "{again}");
}

#[test]
fn a_failed_transit_returns_waiting_messages_to_the_source_backend() {
    let setting = Setting::new("agent-transit-refused");
    let host = setting.start();
    let made = host.post("/agents", json!({"name": "Stayer", "backend": "claude"}));
    let session = made["session"].as_str().unwrap().to_string();
    talked(&host, &session);
    let conversation = row(&host, &session)["conversation_id"].clone();

    // Codex's index has a column this Host cannot fill: the carry fails and takes back what
    // it wrote; the agent stays on its tool, under its name, and the chat says why.
    let index = setting.home.join(".codex").join("state_5.sqlite");
    let db = rusqlite::Connection::open(&index).unwrap();
    db.execute_batch(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
         created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, source TEXT NOT NULL,
         model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
         sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL, unknown_now TEXT NOT NULL);",
    )
    .unwrap();
    drop(db);
    let hold = setting.place.root.join("hold");
    std::fs::write(&hold, "").unwrap();
    host.send(&session, "Take your time.", "bc-2");
    wait_for("the turn to run", || row(&host, &session)["busy"] == true);
    wait_for("source tool work before Transit", || {
        setting.place.root.join("source-held").exists()
    });
    queue_for_transit(
        &host,
        &session,
        "bc-later",
        "Waiting through a failed switch.",
    );
    let ask = json!({"session": session, "backend": "codex", "model": "gpt-5.5",
                     "request_id": "stay-1"});
    let (status, began) = host.ask("POST", "/agent-transit", Some(&ask));
    assert_eq!(status, 202, "{began}");
    std::fs::remove_file(hold).unwrap();
    let failed = finished_transit(&host, &session, began["transit_id"].as_str().unwrap());
    assert_eq!(failed["phase"], "failed", "{failed}");
    assert!(failed["error"].as_str().unwrap().contains("unknown_now"));
    assert!(setting.rollouts().is_empty());
    let after = row(&host, &session);
    assert_eq!(after["backend"], "claude");
    assert_eq!(after["conversation_id"], conversation);
    assert!(
        host.conversation(&session)
            .iter()
            .any(|(role, text)| role == "system"
                && text.starts_with(
                    "Couldn't move to Codex: the conversation couldn't be written for Codex. \
                     Still on Claude"
                )),
        "{:?}",
        host.conversation(&session)
    );

    wait_for("the queue to run on the source", || {
        host.conversation(&session).iter().any(|(role, text)| {
            role == "assistant" && text == "Heard: Waiting through a failed switch."
        })
    });
    wait_for("the source to return to idle", || {
        row(&host, &session)["busy"] == false
    });

    // Asked wrongly: refused before anything is done.
    let refusal = |ask: Value| {
        let (status, said) = host.ask("POST", "/agent-transit", Some(&ask));
        (status, said["error"].as_str().unwrap_or("").to_string())
    };
    let other = |more: Value| {
        let mut ask = json!({"session": session, "request_id": "x"});
        for (key, value) in more.as_object().unwrap() {
            ask[key] = value.clone();
        }
        ask
    };
    assert_eq!(
        refusal(other(json!({"backend": "nothing"}))),
        (400, "unknown_backend".into())
    );
    assert_eq!(
        refusal(other(json!({"backend": "codex", "model": "no-such-model"}))),
        (400, "unknown_model".into())
    );
    assert_eq!(
        refusal(other(json!({"backend": "muse", "model": ""}))),
        (400, "backend_unavailable".into())
    );
    assert_eq!(
        refusal(other(json!({"backend": "claude", "model": ""}))),
        (400, "same_backend_and_model".into())
    );
    let (status, _) = host.ask("GET", "/agent-transit?transit_id=nothing", None);
    assert_eq!(status, 404);
    // And the agent still answers on its own tool.
    host.send(&session, "Still there?", "bc-3");
    wait_for("its answer", || {
        host.conversation(&session)
            .iter()
            .any(|(_, text)| text == "Heard: Still there?")
    });
}
