//! The Antigravity ACP driver (`clarp-shim agy-acp-turn`) against a scripted stand-in for
//! `agy acp` (`tests/fake_acp/agy.py`): what it sends, what it prints, and how it ends.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{Child, Stdio};
use std::time::{Duration, Instant};

use clarp_acp::agy::{DRIVER, Order};
use nix::sys::signal::{Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};

const FAKE: &str = include_str!("fake_acp/agy.py");

struct Place {
    dir: PathBuf,
    agy: PathBuf,
}

impl Place {
    fn new(name: &str, scenario: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-agy-acp-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("tmp")).unwrap();
        std::fs::create_dir_all(dir.join("work")).unwrap();
        let agy = dir.join("agy");
        std::fs::write(&agy, FAKE).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&agy, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(dir.join("scenario"), scenario).unwrap();
        Self { dir, agy }
    }

    fn order(&self, session: &str) -> Order {
        Order {
            session: session.into(),
            cwd: self.dir.join("work").display().to_string(),
            prompt: "hi".into(),
            headed: "HEAD\n\nhi".into(),
            append: "HEAD".into(),
            model: "Claude B".into(),
            effort: "high".into(),
            attachments: Vec::new(),
            silence_ms: 0,
            uuid: String::new(),
        }
    }

    /// Starts the driver with this order on its standard input, as the shim gives it.
    fn start(&self, order: &Order, more: &[(&str, &str)]) -> Child {
        let mut env = clarp_sealed::sealed(&clarp_sealed::runner(), &self.dir, &[], &[]);
        env.insert("GEMINI_API_KEY".into(), "stray".into());
        for (name, value) in more {
            env.insert((*name).into(), (*value).into());
        }
        let mut child = clarp_sealed::command(env!("CARGO_BIN_EXE_clarp-shim"), env)
            .args([DRIVER, self.agy.to_str().unwrap()])
            // In a process group of its own, as the shim starts its tool.
            .process_group(0)
            .current_dir(&self.dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut input = child.stdin.take().unwrap();
        input
            .write_all(order.to_json().to_string().as_bytes())
            .unwrap();
        drop(input);
        child
    }

    /// Runs the driver to its end: what it printed, and how it ended.
    fn run(&self, order: &Order) -> (Vec<Value>, std::process::ExitStatus) {
        let child = self.start(order, &[]);
        let done = child.wait_with_output().unwrap();
        (lines(&done.stdout), done.status)
    }

    /// What the stand-in was sent, in order.
    fn sent(&self) -> Vec<Value> {
        lines(&std::fs::read(self.dir.join("acp.log")).unwrap_or_default())
    }

    fn sent_of(&self, method: &str) -> Vec<Value> {
        self.sent()
            .into_iter()
            .filter(|message| message["method"] == method)
            .collect()
    }
}

impl Drop for Place {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn lines(bytes: &[u8]) -> Vec<Value> {
    String::from_utf8_lossy(bytes)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn driver<'a>(printed: &'a [Value], what: &str) -> Vec<&'a Value> {
    printed
        .iter()
        .filter(|line| line["clarp"] == what)
        .collect()
}

fn updates<'a>(printed: &'a [Value], kind: &str) -> Vec<&'a Value> {
    printed
        .iter()
        .filter(|line| line["params"]["update"]["sessionUpdate"] == kind)
        .collect()
}

#[test]
fn a_turn_opens_a_conversation_chooses_its_model_and_streams_what_antigravity_says() {
    let place = Place::new("turn", "turn");
    let (printed, status) = place.run(&place.order(""));
    assert!(status.success(), "{status:?} {printed:?}");
    assert_eq!(
        driver(&printed, "bound")[0],
        &json!({"clarp": "bound", "session": "conv-new", "resumed": false})
    );
    assert_eq!(driver(&printed, "initialized")[0]["appends"], true);
    assert_eq!(driver(&printed, "model")[0]["model"], "claude-b");
    assert_eq!(updates(&printed, "agent_message_chunk").len(), 2);
    let answered = driver(&printed, "prompted")[0];
    assert_eq!(answered["result"]["stopReason"], "end_turn");
    // How long it took to hear from Antigravity, for the first real turns to give numbers.
    let timing = driver(&printed, "timing")[0];
    assert!(timing["launch_to_first_update_ms"].is_u64(), "{timing}");
    assert!(
        timing["resume_ms"].is_null(),
        "a new conversation resumes nothing"
    );

    // The Host's head went into Antigravity's instructions, and the message is the user's.
    let opened = &place.sent_of("session/new")[0]["params"];
    assert_eq!(
        opened["_meta"]["antigravity"]["systemPrompt"]["append"],
        "HEAD"
    );
    assert_eq!(opened["mcpServers"], json!([]));
    let asked = &place.sent_of("session/prompt")[0]["params"];
    assert_eq!(asked["prompt"], json!([{"type": "text", "text": "hi"}]));
    let chosen: Vec<(Value, Value)> = place
        .sent_of("session/set_config_option")
        .iter()
        .map(|set| {
            (
                set["params"]["configId"].clone(),
                set["params"]["value"].clone(),
            )
        })
        .collect();
    assert_eq!(
        chosen,
        [
            (json!("model"), json!("claude-b")),
            (json!("thought_level"), json!("high"))
        ]
    );
    let init = &place.sent_of("initialize")[0]["params"];
    assert_eq!(init["protocolVersion"], 1);
    assert_eq!(init["clientCapabilities"]["fs"]["writeTextFile"], false);

    // agy is started as an ACP server that asks no permission, without a stray key.
    let argv: Value = serde_json::from_str(
        std::fs::read_to_string(place.dir.join("argv"))
            .unwrap()
            .lines()
            .next()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        argv["argv"],
        json!(["acp", "--dangerously-skip-permissions"])
    );
    assert_eq!(argv["env"]["GEMINI_API_KEY"], Value::Null);
    assert_eq!(argv["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"], "1");
    assert_eq!(argv["env"]["AGY_ACP_FORCE_FILE_STORAGE"], Value::Null);
}

#[test]
fn a_conversation_is_resumed_and_one_antigravity_lost_fails_the_turn_and_is_marked() {
    let place = Place::new("resume", "turn");
    let (printed, status) = place.run(&place.order("conv-old"));
    assert!(status.success());
    assert_eq!(driver(&printed, "bound")[0]["session"], "conv-old");
    assert_eq!(driver(&printed, "bound")[0]["resumed"], true);
    assert!(driver(&printed, "timing")[0]["resume_ms"].is_u64());
    assert_eq!(
        place.sent_of("session/resume")[0]["params"]["sessionId"],
        "conv-old"
    );
    assert_eq!(
        place.sent_of("session/resume")[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"],
        "HEAD"
    );
    assert!(place.sent_of("session/new").is_empty());

    // A conversation agy no longer has is not begun anew without its history: the turn fails,
    // and the mark makes the Host continue it with its own record.
    let lost = Place::new("resume-lost", "resume-lost");
    let (printed, status) = lost.run(&lost.order("conv-gone"));
    assert_eq!(status.code(), Some(1), "{printed:?}");
    let failed = driver(&printed, "failed")[0];
    assert_eq!(failed["stage"], "session");
    assert!(
        failed["text"]
            .as_str()
            .unwrap()
            .contains("Session not found")
    );
    assert!(driver(&printed, "bound").is_empty());
    assert!(lost.sent_of("session/new").is_empty());
    assert!(lost.sent_of("session/prompt").is_empty());
    let mark = clarp_sealed::home(&lost.dir).join(".local/state/clarp/agy-acp/poisoned/conv-gone");
    assert!(mark.exists(), "{}", mark.display());
}

#[test]
fn a_tool_call_and_its_permission_are_answered_and_recorded() {
    let place = Place::new("tool", "tool");
    let (printed, status) = place.run(&place.order(""));
    assert!(status.success());
    assert_eq!(
        updates(&printed, "tool_call")[0]["params"]["update"]["toolCallId"],
        "t-1"
    );
    assert_eq!(updates(&printed, "tool_call_update").len(), 1);
    let permission = driver(&printed, "permission")[0];
    assert_eq!(
        (&permission["kind"], &permission["answer"]),
        (&json!("allow_once"), &json!("Allow"))
    );
    let answer = place
        .sent()
        .into_iter()
        .find(|message| message["id"] == "perm-1")
        .unwrap();
    assert_eq!(
        answer["result"]["outcome"],
        json!({"outcome": "selected", "optionId": "yes"})
    );

    // A question with fixed choices is answered with its first, and the turn goes on: nobody
    // is waited for.
    let question = Place::new("question", "question");
    let (printed, status) = question.run(&question.order(""));
    assert!(status.success());
    assert_eq!(driver(&printed, "permission")[0]["answer"], "Red");
    let answer = question
        .sent()
        .into_iter()
        .find(|message| message["id"] == "perm-1")
        .unwrap();
    assert_eq!(
        answer["result"]["outcome"],
        json!({"outcome": "selected", "optionId": "red"})
    );
}

#[test]
fn attached_files_go_as_pictures_and_links_and_sounds_only_where_taken() {
    let place = Place::new("attach", "turn");
    let uploads = place.dir.join("uploads/s");
    std::fs::create_dir_all(&uploads).unwrap();
    let file = |name: &str, bytes: &[u8]| -> String {
        let path = uploads.join(name);
        std::fs::write(&path, bytes).unwrap();
        path.display().to_string()
    };
    let (png, mp3, pdf) = (
        file("a.png", b"PNG"),
        file("b.mp3", b"ID3"),
        file("c.pdf", b"%PDF"),
    );
    let missing = uploads.join("gone.png").display().to_string();
    let mut order = place.order("");
    order.attachments = vec![png.clone(), mp3, pdf.clone(), missing];
    let (printed, status) = place.run(&order);
    assert!(status.success());
    let blocks = place.sent_of("session/prompt")[0]["params"]["prompt"].clone();
    assert_eq!(blocks[0]["type"], "text");
    assert_eq!(
        blocks[1],
        json!({"type": "image", "mimeType": "image/png", "data": "UE5H"})
    );
    // agy 1.5 takes no audio over ACP: the sound stays a path in the text.
    assert_eq!(blocks[2]["type"], "resource_link");
    assert_eq!(blocks[2]["uri"], format!("file://{pdf}"));
    assert_eq!(blocks.as_array().unwrap().len(), 3);
    let refused: Vec<&Value> = driver(&printed, "not_attached")
        .into_iter()
        .map(|line| &line["why"])
        .collect();
    assert_eq!(refused, [&json!("not taken"), &json!("missing")]);
    assert_eq!(driver(&printed, "attached").len(), 2);

    // An agy that takes audio is given the sound as itself.
    let hearing = Place::new("attach-audio", "turn");
    std::fs::write(hearing.dir.join("audio"), "").unwrap();
    let mut order = hearing.order("");
    order.attachments = vec![mp3_of(&uploads)];
    let (_, status) = hearing.run(&order);
    assert!(status.success());
    let blocks = hearing.sent_of("session/prompt")[0]["params"]["prompt"].clone();
    assert_eq!(blocks[1]["type"], "audio");
    assert_eq!(blocks[1]["mimeType"], "audio/mpeg");
}

fn mp3_of(uploads: &std::path::Path) -> String {
    uploads.join("b.mp3").display().to_string()
}

#[test]
fn a_stop_cancels_the_prompt_and_the_driver_ends_by_signal() {
    let place = Place::new("cancel", "cancel");
    let mut child = place.start(&place.order(""), &[]);
    let mut printed = Vec::new();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    // Once Antigravity is at work, the shim's Stop arrives.
    while output.read_line(&mut line).unwrap() > 0 {
        let said: Value = serde_json::from_str(&line).unwrap();
        line.clear();
        let working = said["params"]["update"]["content"]["text"] == "working";
        printed.push(said);
        if working {
            kill(Pid::from_raw(child.id() as i32), Signal::SIGUSR1).unwrap();
            break;
        }
    }
    for rest in output.lines() {
        printed.push(serde_json::from_str(&rest.unwrap()).unwrap());
    }
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(Signal::SIGTERM as i32), "{status:?}");
    assert_eq!(driver(&printed, "cancel")[0]["session"], "conv-new");
    assert_eq!(
        driver(&printed, "prompted")[0]["result"]["stopReason"],
        "cancelled"
    );
    assert_eq!(
        place.sent_of("session/cancel")[0]["params"]["sessionId"],
        "conv-new"
    );
}

#[test]
fn a_spent_quota_a_bad_signature_and_a_missing_sign_in_are_printed_as_failures() {
    let quota = Place::new("quota", "quota");
    let (printed, status) = quota.run(&quota.order(""));
    assert_eq!(status.code(), Some(1));
    let failed = driver(&printed, "failed")[0];
    assert_eq!(failed["stage"], "prompt");
    assert!(failed["text"].as_str().unwrap().contains("QUOTA_EXHAUSTED"));

    let state = Place::new("signature", "signature");
    let (printed, status) = state.run(&state.order("conv-old"));
    assert_eq!(status.code(), Some(1));
    assert_eq!(driver(&printed, "failed")[0]["stage"], "prompt");
    // The conversation is marked: the Host begins the next turn in a new one.
    let mark = clarp_sealed::home(&state.dir).join(".local/state/clarp/agy-acp/poisoned/conv-old");
    assert!(mark.exists(), "{}", mark.display());

    let signed_out = Place::new("signed-out", "signed-out");
    let (printed, status) = signed_out.run(&signed_out.order(""));
    assert_eq!(status.code(), Some(1));
    let failed = driver(&printed, "failed")[0];
    assert_eq!(failed["stage"], "auth");
    assert_eq!(failed["error"]["code"], -32000);
}

#[test]
fn a_profile_of_its_own_keeps_the_login_in_a_file_there() {
    let place = Place::new("profile", "turn");
    let profile = place.dir.join("profile");
    let child = place.start(
        &place.order(""),
        &[("CLARP_AGY_ACP_PROFILE", profile.to_str().unwrap())],
    );
    assert!(child.wait_with_output().unwrap().status.success());
    let argv: Value = serde_json::from_str(
        std::fs::read_to_string(place.dir.join("argv"))
            .unwrap()
            .lines()
            .next()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        argv["argv"],
        json!([
            "acp",
            "--dangerously-skip-permissions",
            "--gemini-dir",
            profile.to_str().unwrap()
        ])
    );
    assert_eq!(argv["env"]["AGY_ACP_FORCE_FILE_STORAGE"], "1");
    assert_eq!(argv["env"]["SSH_CONNECTION"], "127.0.0.1 0 127.0.0.1 0");
}

#[test]
fn a_model_id_with_its_effort_runs_as_the_model_offered_at_that_thinking_level() {
    let place = Place::new("folded", "turn");
    let mut order = place.order("");
    order.model = "claude-b-high".into();
    order.effort = String::new();
    let (printed, status) = place.run(&order);
    assert!(status.success(), "{printed:?}");
    let chosen: Vec<(Value, Value)> = place
        .sent_of("session/set_config_option")
        .iter()
        .map(|set| {
            (
                set["params"]["configId"].clone(),
                set["params"]["value"].clone(),
            )
        })
        .collect();
    assert_eq!(
        chosen,
        [
            (json!("model"), json!("claude-b")),
            (json!("thought_level"), json!("high"))
        ]
    );
    assert_eq!(driver(&printed, "model")[0]["model"], "claude-b");
    assert!(driver(&printed, "notice").is_empty());
}

#[test]
fn a_model_antigravity_does_not_offer_fails_the_turn_before_anything_is_asked() {
    let place = Place::new("unoffered", "turn");
    let mut order = place.order("");
    order.model = "gemini-z-low".into();
    let (printed, status) = place.run(&order);
    assert_eq!(status.code(), Some(1), "{printed:?}");
    let failed = driver(&printed, "failed")[0];
    assert_eq!(failed["stage"], "config");
    let text = failed["text"].as_str().unwrap();
    assert!(
        text.contains("gemini-z-low") && text.contains("gemini-a, claude-b"),
        "{text}"
    );
    assert!(
        place.sent_of("session/prompt").is_empty(),
        "no other model runs in its place"
    );
    assert!(place.sent_of("session/set_config_option").is_empty());
}

/// Runs the driver to its end, with how long it took.
fn timed(place: &Place, order: &Order) -> (Vec<Value>, std::process::ExitStatus, Duration) {
    let began = Instant::now();
    let (printed, status) = place.run(order);
    (printed, status, began.elapsed())
}

#[test]
fn a_silent_prompt_ends_the_turn_but_a_running_tool_may_take_its_time() {
    // No tool call open: silence past the bound ends the turn.
    let stalled = Place::new("slow", "slow");
    let mut order = stalled.order("");
    order.silence_ms = 300;
    let (printed, status, _) = timed(&stalled, &order);
    assert_eq!(status.code(), Some(1), "{printed:?}");
    let failed = driver(&printed, "failed")[0];
    assert_eq!(
        (&failed["stage"], &failed["silent"]),
        (&json!("prompt"), &json!(true))
    );

    // A tool call that runs longer than the bound says nothing until it ends, and the turn waits.
    let building = Place::new("slow-tool", "slow-tool");
    let mut order = building.order("");
    order.silence_ms = 300;
    let (printed, status, _) = timed(&building, &order);
    assert!(status.success(), "{printed:?}");
    assert_eq!(
        driver(&printed, "prompted")[0]["result"]["stopReason"],
        "end_turn"
    );
    assert!(driver(&printed, "failed").is_empty());
}

#[test]
fn a_message_too_long_for_one_spool_line_is_printed_cut_and_whole() {
    let place = Place::new("big", "big");
    let (printed, status) = place.run(&place.order(""));
    assert!(status.success(), "{printed:?}");
    let ended = updates(&printed, "tool_call_update")[0];
    let line = serde_json::to_vec(ended).unwrap();
    assert!(
        line.len() <= clarp_acp::schema::PRINTED_MOST,
        "{}",
        line.len()
    );
    let update = &ended["params"]["update"];
    assert_eq!(
        (&update["toolCallId"], &update["status"]),
        (&json!("t-big"), &json!("completed"))
    );
    assert!(
        update["content"][0]["content"]["data"]
            .as_str()
            .unwrap()
            .ends_with("bytes left out]")
    );
    assert!(
        update["rawOutput"]["combinedOutput"]
            .as_str()
            .unwrap()
            .ends_with("[cut]")
    );
}

#[test]
fn what_agy_leaves_behind_holding_standard_error_is_ended_with_the_turn() {
    let place = Place::new("orphan", "orphan");
    // The child sleeps two minutes and holds the driver's standard error: the turn must not wait
    // for it.
    let (printed, status, took) = timed(&place, &place.order(""));
    assert!(status.success(), "{printed:?}");
    assert!(
        took < Duration::from_secs(60),
        "the turn waited {took:?} for agy's child"
    );
}

#[test]
fn a_permission_asked_after_a_stop_is_declined() {
    let place = Place::new("cancel-ask", "cancel-ask");
    let mut child = place.start(&place.order(""), &[]);
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut printed = Vec::new();
    let mut line = String::new();
    while output.read_line(&mut line).unwrap() > 0 {
        let said: Value = serde_json::from_str(&line).unwrap();
        line.clear();
        let working = said["params"]["update"]["content"]["text"] == "working";
        printed.push(said);
        if working {
            kill(Pid::from_raw(child.id() as i32), Signal::SIGUSR1).unwrap();
            break;
        }
    }
    for rest in output.lines() {
        printed.push(serde_json::from_str(&rest.unwrap()).unwrap());
    }
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(Signal::SIGTERM as i32), "{status:?}");
    let permission = driver(&printed, "permission")[0];
    assert_eq!(
        (&permission["answer"], &permission["stopped"]),
        (&Value::Null, &json!(true))
    );
    let answer = place
        .sent()
        .into_iter()
        .find(|message| message["id"] == "perm-1")
        .unwrap();
    assert_eq!(answer["result"]["outcome"], json!({"outcome": "cancelled"}));
}
