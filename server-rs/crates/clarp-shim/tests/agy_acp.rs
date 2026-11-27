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
            conversation: false,
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
        let (child, input) = self.open(order, more);
        drop(input);
        child
    }

    /// Starts the driver with its order as the first line of an input that stays open, as the
    /// shim keeps it for a turn that may be steered: the Host's messages are written after it.
    fn open(&self, order: &Order, more: &[(&str, &str)]) -> (Child, std::process::ChildStdin) {
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
            // One line: the Host's messages, if any, are the lines after it.
            .write_all(format!("{}\n", order.to_json()).as_bytes())
            .unwrap();
        input.flush().unwrap();
        (child, input)
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
fn a_conversation_opens_a_fresh_acp_session_and_declines_tool_requests() {
    let place = Place::new("conversation", "tool");
    let mut order = place.order("conv-old");
    order.conversation = true;
    let (printed, status) = place.run(&order);
    assert!(status.success(), "{printed:?}");
    assert!(place.sent_of("session/resume").is_empty());
    assert_eq!(place.sent_of("session/new").len(), 1);
    let permission = place
        .sent()
        .into_iter()
        .find(|message| message["id"] == "perm-1")
        .unwrap();
    assert_eq!(
        permission["result"]["outcome"],
        json!({"outcome":"cancelled"})
    );
    let argv: Value = serde_json::from_str(
        std::fs::read_to_string(place.dir.join("argv"))
            .unwrap()
            .lines()
            .next()
            .unwrap(),
    )
    .unwrap();
    let argv = argv["argv"].as_array().unwrap();
    assert_eq!(argv[0], "--settings");
    assert_eq!(argv[2..], [json!("acp"), json!("--sandbox")]);
    let settings: Value = serde_json::from_str(argv[1].as_str().unwrap()).unwrap();
    assert_eq!(settings["toolPermission"], "strict");
    assert!(
        settings["permissions"]["deny"]
            .as_array()
            .unwrap()
            .contains(&json!("write_file(*)"))
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

#[test]
fn what_an_agent_left_running_on_purpose_outlives_the_turn() {
    let place = Place::new("detached", "detached");
    let (printed, status) = place.run(&place.order(""));
    assert!(status.success(), "{printed:?}");
    let pids: Vec<i32> = std::fs::read_to_string(place.dir.join("detached.pids"))
        .unwrap()
        .split_whitespace()
        .map(|pid| pid.parse().unwrap())
        .collect();
    assert_eq!(pids.len(), 2);
    // Neither holds the turn's pipes: a server in a session of its own and one `nohup`-style in
    // agy's group both go on after the turn has ended.
    let alive: Vec<bool> = pids
        .iter()
        .map(|pid| {
            // Running or sleeping, not ended and waiting to be reaped.
            std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .and_then(|stat| {
                    stat.rsplit_once(')')
                        .map(|(_, after)| after.trim_start().to_string())
                })
                .is_some_and(|after| !after.starts_with('Z'))
        })
        .collect();
    for pid in &pids {
        let _ = kill(Pid::from_raw(*pid), Signal::SIGKILL);
    }
    assert_eq!(alive, [true, true], "{pids:?}");
}

/// What the driver prints, read on a thread of its own as it comes.
struct Printed {
    lines: std::sync::mpsc::Receiver<Value>,
    seen: Vec<Value>,
}

impl Printed {
    fn of(child: &mut Child) -> Self {
        let output = BufReader::new(child.stdout.take().unwrap());
        let (tell, lines) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for line in output.lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                if tell.send(serde_json::from_str(&line).unwrap()).is_err() {
                    break;
                }
            }
        });
        Self {
            lines,
            seen: Vec::new(),
        }
    }

    /// Reads on until a line says `until`.
    fn until(&mut self, what: &str, until: impl Fn(&Value) -> bool) {
        let end = Instant::now() + Duration::from_secs(30);
        while !self.seen.iter().any(&until) {
            let left = end.saturating_duration_since(Instant::now());
            let line = self
                .lines
                .recv_timeout(left)
                .unwrap_or_else(|_| panic!("{what}: {:?}", self.seen));
            self.seen.push(line);
        }
    }

    /// Reads to the end.
    fn rest(&mut self) {
        while let Ok(line) = self.lines.recv_timeout(Duration::from_secs(30)) {
            self.seen.push(line);
        }
    }

    fn at(&self, what: impl Fn(&Value) -> bool) -> usize {
        self.seen
            .iter()
            .position(what)
            .unwrap_or_else(|| panic!("not printed: {:?}", self.seen))
    }
}

fn wait_file(place: &Place, name: &str) {
    let end = Instant::now() + Duration::from_secs(30);
    while !place.dir.join(name).exists() {
        assert!(Instant::now() < end, "{name}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// The Host's line for a message sent while the turn runs.
fn steer(input: &mut std::process::ChildStdin, uuid: &str, text: &str) {
    let line = json!({"type": "user", "uuid": uuid,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]}});
    writeln!(input, "{line}").unwrap();
    input.flush().unwrap();
}

fn read_of(line: &Value) -> Option<&str> {
    clarp_acp::agy::steer::read_of(line)
}

fn settled(printed: &[Value]) -> Vec<&Value> {
    printed
        .iter()
        .filter(|line| line["type"] == "result")
        .collect()
}

/// The driver sends a message that comes while agy writes at once, as during a tool: the read
/// of a reply agy goes on writing after it is what such a test is about.
const NOT_CUT: (&str, &str) = (clarp_acp::agy::steer::CUT_ENV, "off");

fn steering(place: &Place, case: &str) -> Order {
    let mut order = place.order("");
    order.prompt = case.into();
    order.uuid = "u-own".into();
    order
}

#[test]
fn a_message_sent_while_a_prompt_runs_is_read_when_antigravity_works_on_it_and_answered_with_it() {
    for mode in ["tool", "writing"] {
        let place = Place::new(&format!("steer-{mode}"), "steer");
        // agy writes in one mode: the message goes as it goes during a tool, not cut.
        let (mut child, mut input) =
            place.open(&steering(&place, &format!("case-{mode}")), &[NOT_CUT]);
        let mut printed = Printed::of(&mut child);
        wait_file(&place, &format!("working-{mode}"));
        steer(&mut input, "u-add", &format!("add-{mode}"));
        wait_file(&place, &format!("received-add-{mode}"));
        // agy answered the running prompt at once; the message is not read on the sending.
        printed.until("the steer sent", |line| line["clarp"] == "steer");
        // The hand-over answer comes at once and is printed as nothing: give it the time.
        std::thread::sleep(Duration::from_millis(200));
        while let Ok(line) = printed.lines.try_recv() {
            printed.seen.push(line);
        }
        // What agy says by itself right after the second prompt is no read either.
        printed.until("agy's own usage update", |line| {
            line["params"]["update"]["sessionUpdate"] == "usage_update"
        });
        assert!(
            printed.seen.iter().all(|line| read_of(line).is_none()),
            "{:?}",
            printed.seen
        );
        std::fs::write(place.dir.join(format!("taken-{mode}")), "").unwrap();
        printed.until("the read", |line| read_of(line) == Some("u-add"));
        // And what follows it, which may not have been read yet.
        printed.until("agy's first word under the new prompt", |line| {
            line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
        });
        let read = printed.at(|line| read_of(line) == Some("u-add"));
        assert_eq!(
            printed
                .seen
                .get(read + 1)
                .map(|line| &line["params"]["update"]["sessionUpdate"]),
            Some(&json!("agent_thought_chunk")),
            "the read comes just before Antigravity's first word under the new prompt"
        );
        std::fs::write(place.dir.join(format!("release-{mode}")), "").unwrap();
        printed.until("the settled answer", |line| line["type"] == "result");
        drop(input);
        printed.rest();
        assert!(child.wait().unwrap().success());
        let seen = &printed.seen;
        let read = printed.at(|line| read_of(line) == Some("u-add"));
        let after = printed
            .at(|line| line["params"]["update"]["content"]["text"] == format!("after-{mode}"));
        assert!(
            read < after,
            "the read comes before the work for it: {seen:?}"
        );
        if mode == "tool" {
            let ended = printed.at(|line| line["params"]["update"]["status"] == "completed");
            assert!(read < ended, "{seen:?}");
        }
        // One answer, covering both, with what both prompts spent.
        let answers = settled(seen);
        assert_eq!(answers.len(), 1, "{seen:?}");
        assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
        assert_eq!(answers[0]["is_error"], false);
        assert!(
            answers[0]["result"]
                .as_str()
                .unwrap()
                .contains(&format!("after-{mode}"))
        );
        let prompted = driver(seen, "prompted");
        assert_eq!(prompted.len(), 1);
        // The answer is the newer prompt's and covers the turn's own message: it is told as the
        // turn's own answer, so the turn keeps how it stopped, and its result names both.
        assert_eq!(prompted[0]["uuid"], "u-own");
        assert!(
            printed.at(|line| line["clarp"] == "prompted")
                < printed.at(|line| line["type"] == "result")
        );
        assert_eq!(
            prompted[0]["result"]["usage"],
            json!({"inputTokens": 13, "outputTokens": 6})
        );
        let prompts = place.sent_of("session/prompt");
        assert_eq!(prompts.len(), 2);
        assert_eq!(prompts[1]["params"]["sessionId"], "conv-new");
        assert_eq!(
            prompts[1]["params"]["prompt"],
            json!([{"type": "text", "text": format!("add-{mode}")}])
        );
    }
}

#[test]
fn a_message_antigravity_refuses_is_never_read_and_the_first_answer_stands() {
    let place = Place::new("steer-refused", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-refused"), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-refused");
    // The turn's own message is never taken in again: only the steered one is sent.
    steer(&mut input, "u-own", "case-refused");
    steer(&mut input, "u-add", "add-refused");
    wait_file(&place, "received-add-refused");
    // Given twice, it is sent once.
    steer(&mut input, "u-add", "add-refused");
    std::fs::write(place.dir.join("release-refused"), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let seen = &printed.seen;
    assert!(seen.iter().all(|line| read_of(line).is_none()), "{seen:?}");
    let answers = settled(seen);
    assert_eq!(answers.len(), 1, "{seen:?}");
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own"]));
    let refused = seen
        .iter()
        .find(|line| line["type"] == "steer_refused")
        .unwrap();
    assert_eq!(refused["uuid"], "u-add");
    // Nothing that reads as an answer names it: only the record of its sending and refusal.
    assert!(
        seen.iter()
            .filter(|line| line.to_string().contains("u-add"))
            .all(|line| line["clarp"] == "steer" || line["type"] == "steer_refused"),
        "{seen:?}"
    );
    assert_eq!(driver(seen, "prompted")[0]["uuid"], "u-own");
    assert_eq!(place.sent_of("session/prompt").len(), 2);
    assert!(seen.iter().all(|line| read_of(line).is_none()));
}

#[test]
fn a_stop_ends_one_message_read_and_leaves_one_unread() {
    let place = Place::new("steer-stop", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-stop"), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-stop");
    steer(&mut input, "u-1", "add-stop-1");
    wait_file(&place, "received-add-stop-1");
    std::fs::write(place.dir.join("taken-stop"), "").unwrap();
    printed.until("the first message read", |line| {
        read_of(line) == Some("u-1")
    });
    steer(&mut input, "u-2", "add-stop-2");
    wait_file(&place, "received-add-stop-2");
    kill(Pid::from_raw(child.id() as i32), Signal::SIGUSR1).unwrap();
    printed.rest();
    drop(input);
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(Signal::SIGTERM as i32), "{status:?}");
    let seen = &printed.seen;
    let reads: Vec<&str> = seen.iter().filter_map(read_of).collect();
    assert_eq!(reads, ["u-1"], "{seen:?}");
    // What is said after the Stop names nothing that was not read.
    for answer in settled(seen) {
        assert_eq!(
            answer["user_message_uuids"],
            json!(["u-own", "u-1"]),
            "{seen:?}"
        );
    }
    // Cancelled once, and again if the prompt sent before the Stop became agy's next.
    assert!(!place.sent_of("session/cancel").is_empty());
    assert_eq!(place.sent_of("session/prompt").len(), 3);
}

#[test]
fn a_message_that_comes_after_the_answer_is_answered_as_a_prompt_of_its_own() {
    let place = Place::new("steer-after", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-after"), &[]);
    let mut printed = Printed::of(&mut child);
    printed.until("the first answer", |line| line["type"] == "result");
    // The Host wrote it before it read that answer: the shim closes the input on that line.
    steer(&mut input, "u-late", "add-after");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let seen = &printed.seen;
    let answers = settled(seen);
    assert_eq!(answers.len(), 2, "{seen:?}");
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own"]));
    assert_eq!(answers[1]["user_message_uuids"], json!(["u-late"]));
    let prompted = driver(seen, "prompted");
    assert_eq!(
        (&prompted[0]["uuid"], &prompted[1]["uuid"]),
        (&json!("u-own"), &json!("u-late"))
    );
    assert_eq!(driver(seen, "bound")[0]["uuid"], "u-own");
    let first = printed.at(|line| line["type"] == "result");
    let read = printed.at(|line| read_of(line) == Some("u-late"));
    let later =
        printed.at(|line| line["params"]["update"]["content"]["text"] == "separate-add-after");
    assert!(first < read && read < later, "{seen:?}");
    assert_eq!(driver(seen, "prompted").len(), 2);
}

#[test]
fn a_turn_that_fails_before_its_prompt_settles_its_own_message_and_never_waits_for_the_input() {
    let place = Place::new("steer-signed-out", "signed-out");
    let mut order = place.order("");
    order.uuid = "u-own".into();
    // The input stays open, as the shim keeps it for a turn that may be steered: the shim
    // closes it only on a settled line, so the driver must print one and leave by itself.
    let (mut child, input) = place.open(&order, &[]);
    let mut printed = Printed::of(&mut child);
    printed.rest();
    let status = child.wait().unwrap();
    drop(input);
    assert_eq!(status.code(), Some(1), "{:?}", printed.seen);
    assert_eq!(driver(&printed.seen, "failed")[0]["stage"], "auth");
    let answers = settled(&printed.seen);
    assert_eq!(answers.len(), 1, "{:?}", printed.seen);
    assert_eq!(answers[0]["is_error"], true);
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own"]));
    // In the failure's own words, not a word of the driver's.
    let said = &driver(&printed.seen, "failed")[0]["text"];
    assert!(said.as_str().is_some_and(|said| !said.is_empty()));
    assert_eq!(&answers[0]["result"], said);
}

#[test]
fn what_antigravity_says_by_itself_is_no_read_of_a_message() {
    let place = Place::new("steer-meta", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-after"), &[]);
    let mut printed = Printed::of(&mut child);
    printed.until("the first answer", |line| line["type"] == "result");
    // Sent where nothing runs: agy says which commands it offers, then refuses it.
    steer(&mut input, "u-meta", "add-meta");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let seen = &printed.seen;
    assert!(
        seen.iter()
            .any(|line| line["params"]["update"]["sessionUpdate"] == "available_commands_update"),
        "{seen:?}"
    );
    assert!(seen.iter().all(|line| read_of(line).is_none()), "{seen:?}");
    let refused = seen
        .iter()
        .find(|line| line["type"] == "steer_refused")
        .unwrap();
    assert_eq!(refused["uuid"], "u-meta");
    let answers = settled(seen);
    assert_eq!(answers.len(), 1, "{seen:?}");
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own"]));
}

#[test]
fn a_command_begun_before_the_message_that_ends_after_it_is_no_read() {
    let place = Place::new("steer-background", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-background"), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-background");
    steer(&mut input, "u-add", "add-background");
    // agy hands the work over, then the command the first prompt began ends by itself.
    printed.until("the earlier command's end", |line| {
        line["params"]["update"]["status"] == "completed"
    });
    assert!(
        printed.seen.iter().all(|line| read_of(line).is_none()),
        "an update agy sends on its own is no read: {:?}",
        printed.seen
    );
    // The next update under the new prompt is.
    std::fs::write(place.dir.join("taken-background"), "").unwrap();
    printed.until("agy's first word under the new prompt", |line| {
        line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    });
    let read = printed.at(|line| read_of(line) == Some("u-add"));
    let ended = printed.at(|line| line["params"]["update"]["status"] == "completed");
    let first =
        printed.at(|line| line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk");
    assert!(ended < read && read + 1 == first, "{:?}", printed.seen);
    std::fs::write(place.dir.join("release-background"), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let answers = settled(&printed.seen);
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
}

#[test]
fn a_sentence_agy_goes_on_writing_after_the_hand_over_is_whole_before_the_read() {
    let place = Place::new("steer-sentence", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-sentence"), &[NOT_CUT]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-sentence");
    steer(&mut input, "u-add", "add-sentence");
    wait_file(&place, "received-add-sentence");
    std::fs::write(place.dir.join("taken-sentence"), "").unwrap();
    printed.until("agy's first thought under the new prompt", |line| {
        line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    });
    let rest = printed.at(|line| line["params"]["update"]["content"]["text"] == " 4 is 9");
    let read = printed.at(|line| read_of(line) == Some("u-add"));
    let thought =
        printed.at(|line| line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk");
    // "5 plus" and " 4 is 9" are one reply; the message is read after it, before the thought.
    assert!(rest < read && read + 1 == thought, "{:?}", printed.seen);
    std::fs::write(place.dir.join("release-sentence"), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let answers = settled(&printed.seen);
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
}

#[test]
fn a_sentence_that_goes_on_to_the_new_prompts_answer_is_read_before_that_answer() {
    let place = Place::new("steer-closing", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-closing"), &[NOT_CUT]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-closing");
    steer(&mut input, "u-add", "add-closing");
    wait_file(&place, "received-add-closing");
    std::fs::write(place.dir.join("taken-closing"), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let rest = printed.at(|line| line["params"]["update"]["content"]["text"] == " 4 is 9");
    let read = printed.at(|line| read_of(line) == Some("u-add"));
    let prompted = printed.at(|line| line["clarp"] == "prompted");
    // Nothing but the rest of the sentence came: the read is printed with the new prompt's
    // answer, after the whole sentence and before the answer's lines.
    assert!(rest < read && read + 1 == prompted, "{:?}", printed.seen);
    let answers = settled(&printed.seen);
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
}

/// agy goes on writing after the hand-over and answers the message inside that text: the
/// pieces it printed from the turn of `case`, until the settled answer.
fn written_through_the_hand_over(case: &str) -> Vec<Value> {
    let place = Place::new(&format!("steer-{case}"), "steer");
    let (mut child, mut input) = place.open(&steering(&place, &format!("case-{case}")), &[NOT_CUT]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, &format!("working-{case}"));
    steer(&mut input, "u-add", &format!("add-{case}"));
    wait_file(&place, &format!("received-add-{case}"));
    std::fs::write(place.dir.join(format!("taken-{case}")), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    let answers = settled(&printed.seen);
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
    printed.seen
}

/// What was printed, as the Host reads it: the words of each piece, `<read>` for the message's
/// read and `<prompted>` for the answer.
fn as_read(seen: &[Value]) -> Vec<String> {
    seen.iter()
        .filter_map(|line| {
            if read_of(line) == Some("u-add") {
                Some("<read>".to_string())
            } else if line["clarp"] == "prompted" {
                Some("<prompted>".to_string())
            } else if line["params"]["update"]["sessionUpdate"] == "agent_message_chunk" {
                line["params"]["update"]["content"]["text"]
                    .as_str()
                    .map(str::to_string)
            } else {
                None
            }
        })
        .collect()
}

#[test]
fn a_message_agy_answers_in_the_text_it_goes_on_writing_is_read_at_the_paragraph_break() {
    let seen = written_through_the_hand_over("paragraph");
    // The piece with the break is printed in two: up to and with the break, the read, the rest.
    assert_eq!(
        as_read(&seen),
        [
            "5 plus",
            " 4 is 9.\n\n",
            "<read>",
            "7 times",
            " 6 is 42.",
            "<prompted>"
        ],
        "{seen:?}"
    );
}

#[test]
fn a_paragraph_break_across_two_pieces_takes_the_read_after_it() {
    let seen = written_through_the_hand_over("straddle");
    // The break began with the piece before the hand-over and ends with the first after it.
    assert_eq!(
        as_read(&seen),
        [
            "5 plus 4 is 9.\n",
            "\n",
            "<read>",
            "7 times 6 is 42.",
            "<prompted>"
        ],
        "{seen:?}"
    );
}

#[test]
fn lines_with_no_paragraph_break_are_read_at_the_new_prompts_answer() {
    let seen = written_through_the_hand_over("lines");
    // No paragraph break came: the read waits for the answer, as before.
    assert_eq!(
        as_read(&seen),
        ["one\ntwo", "\nthree\n", "four", "<read>", "<prompted>"],
        "{seen:?}"
    );
}

#[test]
fn a_blank_line_in_a_code_block_is_passed_over_and_the_read_goes_after_the_block() {
    let seen = written_through_the_hand_over("fence");
    // The block opened before the hand-over; its blank line is no break, the one after it is.
    assert_eq!(
        as_read(&seen),
        [
            "Here:\n```sh\necho a\n",
            "\necho b\n```\n\n",
            "<read>",
            "The answer",
            " is 42.",
            "<prompted>"
        ],
        "{seen:?}"
    );
}

/// A reply cut for a message in the turn of `case`: the stand-in writes its first piece, the
/// message comes, and once the driver holds it the stand-in goes on (`go-<case>`). What the
/// driver printed, and the prompt the message went as.
fn cut_for_a_message(case: &str) -> (Vec<Value>, String) {
    let place = Place::new(&format!("cut-{case}"), "steer");
    let (mut child, mut input) = place.open(&steering(&place, &format!("case-{case}")), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, &format!("working-{case}"));
    steer(&mut input, "u-add", &format!("add-{case}"));
    printed.until("the message held", |line| {
        line["clarp"] == "cut" && line["stage"] == "held"
    });
    std::fs::write(place.dir.join(format!("go-{case}")), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success(), "{:?}", printed.seen);
    let seen = printed.seen;
    // One answer for both, as when agy takes a message over: no Stop, and nothing new.
    let answers = settled(&seen);
    assert_eq!(answers.len(), 1, "{seen:?}");
    assert_eq!(answers[0]["user_message_uuids"], json!(["u-own", "u-add"]));
    assert_eq!(answers[0]["is_error"], false);
    let prompted = driver(&seen, "prompted");
    assert_eq!(prompted.len(), 1, "{seen:?}");
    assert_eq!(prompted[0]["uuid"], "u-own");
    assert_eq!(prompted[0]["result"]["stopReason"], "end_turn");
    assert_eq!(
        prompted[0]["result"]["usage"],
        json!({"inputTokens": 13, "outputTokens": 6})
    );
    assert!(
        driver(&seen, "cancel").is_empty(),
        "a cut is no Stop: {seen:?}"
    );
    assert_eq!(place.sent_of("session/cancel").len(), 1);
    let prompts = place.sent_of("session/prompt");
    assert_eq!(prompts.len(), 2, "{seen:?}");
    let given = std::fs::read_to_string(place.dir.join(format!("prompt-{case}"))).unwrap();
    // The read comes just before agy's first word for the message, after the cut.
    let cut = printed_at(&seen, |line| {
        line["clarp"] == "cut" && line["stage"] == "cancel"
    });
    let read = printed_at(&seen, |line| read_of(line) == Some("u-add"));
    let banana = printed_at(&seen, |line| {
        line["params"]["update"]["content"]["text"] == "BANANA"
    });
    assert!(cut < read && read + 1 == banana, "{seen:?}");
    (seen, given)
}

fn printed_at(seen: &[Value], what: impl Fn(&Value) -> bool) -> usize {
    seen.iter()
        .position(what)
        .unwrap_or_else(|| panic!("{seen:?}"))
}

/// The pieces of the reply printed, in order.
fn pieces(seen: &[Value]) -> Vec<&str> {
    seen.iter()
        .filter(|line| line["params"]["update"]["sessionUpdate"] == "agent_message_chunk")
        .filter_map(|line| line["params"]["update"]["content"]["text"].as_str())
        .collect()
}

#[test]
fn a_message_that_comes_while_agy_writes_cuts_the_reply_at_the_next_line_end() {
    let (seen, given) = cut_for_a_message("list");
    // The reply ends at the line end; what agy wrote after it, before and after the cancel
    // landed, is left out, as agy itself forgets it.
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men\n", "BANANA"]);
    let sent = driver(&seen, "cut")
        .into_iter()
        .find(|line| line["stage"] == "sent")
        .unwrap();
    assert_eq!(sent["given_back"], 16);
    assert_eq!(sent["left_out"], "3. Udon".len() + "late".len());
    // The message goes with the reply so far before it, marked at both ends.
    assert_eq!(
        given,
        clarp_acp::agy::steer::with_reply("1. Pho\n2. Ramen\n", "add-list")
    );
}

#[test]
fn a_cut_inside_a_code_block_waits_for_the_line_that_closes_it() {
    let (seen, given) = cut_for_a_message("blockend");
    assert_eq!(
        pieces(&seen),
        ["Here:\n```sh\necho a\n", "echo b\n```\n", "BANANA"]
    );
    assert_eq!(
        given,
        clarp_acp::agy::steer::with_reply("Here:\n```sh\necho a\necho b\n```\n", "add-blockend")
    );
}

#[test]
fn a_code_block_that_does_not_close_is_cut_at_the_bound() {
    let began = Instant::now();
    let (seen, given) = cut_for_a_message("blockopen");
    // No line end outside the block came: the reply is cut where it was, after the bound.
    assert!(began.elapsed() >= Duration::from_secs(3));
    assert_eq!(pieces(&seen), ["```\nx\n", "y\n", "BANANA"]);
    assert_eq!(
        given,
        clarp_acp::agy::steer::with_reply("```\nx\ny\n", "add-blockopen")
    );
}

#[test]
fn a_message_during_a_tool_is_not_cut() {
    let place = Place::new("cut-tool", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-tool"), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-tool");
    steer(&mut input, "u-add", "add-tool");
    // Sent at once, as it is: agy takes it over from the prompt that runs the tool.
    wait_file(&place, "received-add-tool");
    std::fs::write(place.dir.join("taken-tool"), "").unwrap();
    std::fs::write(place.dir.join("release-tool"), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success());
    assert!(
        driver(&printed.seen, "cut").is_empty(),
        "{:?}",
        printed.seen
    );
    assert!(place.sent_of("session/cancel").is_empty());
    assert_eq!(
        settled(&printed.seen)[0]["user_message_uuids"],
        json!(["u-own", "u-add"])
    );
}

#[test]
fn a_stop_while_a_message_is_held_ends_the_turn_and_the_message_is_not_taken() {
    let place = Place::new("cut-holdstop", "steer");
    let (mut child, mut input) = place.open(&steering(&place, "case-holdstop"), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, "working-holdstop");
    steer(&mut input, "u-add", "add-holdstop");
    printed.until("the message held", |line| {
        line["clarp"] == "cut" && line["stage"] == "held"
    });
    kill(Pid::from_raw(child.id() as i32), Signal::SIGUSR1).unwrap();
    printed.until("the Stop's cancel", |line| line["clarp"] == "cancel");
    std::fs::write(place.dir.join("go-holdstop"), "").unwrap();
    printed.rest();
    drop(input);
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(Signal::SIGTERM as i32), "{status:?}");
    let seen = &printed.seen;
    assert!(seen.iter().all(|line| read_of(line).is_none()), "{seen:?}");
    assert!(
        seen.iter()
            .any(|line| line["type"] == "steer_refused" && line["uuid"] == "u-add"),
        "{seen:?}"
    );
    for answer in settled(seen) {
        assert_eq!(answer["user_message_uuids"], json!(["u-own"]), "{seen:?}");
    }
    // The message never went: the only prompt is the turn's own.
    assert_eq!(place.sent_of("session/prompt").len(), 1);
}

/// A message held while the stand-in writes in the turn of `case`; `more` is done once the
/// driver holds it, before the stand-in goes on (`go-<case>`). What the driver printed, the
/// prompts the stand-in kept, and the stand-in's place.
fn held_for(
    case: &str,
    more: impl FnOnce(&mut std::process::ChildStdin, &mut Printed),
) -> (Vec<Value>, Place) {
    let place = Place::new(&format!("held-{case}"), "steer");
    let (mut child, mut input) = place.open(&steering(&place, &format!("case-{case}")), &[]);
    let mut printed = Printed::of(&mut child);
    wait_file(&place, &format!("working-{case}"));
    steer(&mut input, "u-add", &format!("add-{case}"));
    printed.until("the message held", |line| {
        line["clarp"] == "cut" && line["stage"] == "held"
    });
    more(&mut input, &mut printed);
    std::fs::write(place.dir.join(format!("go-{case}")), "").unwrap();
    printed.until("the settled answer", |line| line["type"] == "result");
    drop(input);
    printed.rest();
    assert!(child.wait().unwrap().success(), "{:?}", printed.seen);
    // Never a Stop.
    assert!(
        driver(&printed.seen, "cancel").is_empty(),
        "{:?}",
        printed.seen
    );
    let prompted = driver(&printed.seen, "prompted");
    assert_eq!(prompted.len(), 1, "{:?}", printed.seen);
    assert_eq!(prompted[0]["result"]["stopReason"], "end_turn");
    (printed.seen, place)
}

fn kept_prompt(place: &Place, name: &str) -> String {
    std::fs::read_to_string(place.dir.join(format!("prompt-{name}"))).unwrap()
}

#[test]
fn a_cancel_answered_after_the_bound_sends_the_message_and_keeps_the_rows_apart() {
    let began = Instant::now();
    let (seen, place) = held_for("slow", |_, _| {});
    // The message went at the bound, with the reply given back; what agy wrote for the
    // cancelled prompt until its late answer is left out, and the answer is a row of its own.
    assert!(began.elapsed() >= Duration::from_secs(8));
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men\n", "BANANA"]);
    assert_eq!(
        kept_prompt(&place, "slow"),
        clarp_acp::agy::steer::with_reply("1. Pho\n2. Ramen\n", "add-slow")
    );
    let read = printed_at(&seen, |line| read_of(line) == Some("u-add"));
    let banana = printed_at(&seen, |line| {
        line["params"]["update"]["content"]["text"] == "BANANA"
    });
    assert_eq!(read + 1, banana, "{seen:?}");
    assert_eq!(
        settled(&seen)[0]["user_message_uuids"],
        json!(["u-own", "u-add"])
    );
}

#[test]
fn a_message_refused_after_a_late_cancel_leaves_the_turn_ended_not_stopped() {
    let (seen, _place) = held_for("slowrefused", |_, _| {});
    assert!(seen.iter().all(|line| read_of(line).is_none()), "{seen:?}");
    assert!(
        seen.iter()
            .any(|line| line["type"] == "steer_refused" && line["uuid"] == "u-add"),
        "{seen:?}"
    );
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men\n"]);
    assert_eq!(settled(&seen)[0]["user_message_uuids"], json!(["u-own"]));
}

#[test]
fn a_thought_before_any_line_end_sends_the_message_without_a_cut() {
    let (seen, place) = held_for("missed", |_, _| {});
    assert!(place.sent_of("session/cancel").is_empty());
    assert!(
        driver(&seen, "cut")
            .iter()
            .all(|line| line["stage"] == "held"),
        "{seen:?}"
    );
    // As a message sent while nothing is cut: as the Host wrote it.
    assert_eq!(kept_prompt(&place, "missed"), "add-missed");
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "BANANA"]);
    let read = printed_at(&seen, |line| read_of(line) == Some("u-add"));
    let banana = printed_at(&seen, |line| {
        line["params"]["update"]["content"]["text"] == "BANANA"
    });
    assert_eq!(read + 1, banana, "{seen:?}");
    assert_eq!(
        settled(&seen)[0]["user_message_uuids"],
        json!(["u-own", "u-add"])
    );
}

#[test]
fn a_reply_that_ends_its_own_prompt_before_a_line_end_sends_the_message_as_it_was() {
    let (seen, place) = held_for("ends", |_, _| {});
    assert!(place.sent_of("session/cancel").is_empty());
    assert_eq!(kept_prompt(&place, "ends"), "add-ends");
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men", "BANANA"]);
    assert_eq!(
        settled(&seen)[0]["user_message_uuids"],
        json!(["u-own", "u-add"])
    );
}

#[test]
fn a_reply_that_already_ends_a_line_is_cut_at_once() {
    let (seen, place) = held_for("linestart", |_, printed| {
        // Before agy writes another word.
        printed.until("the cut", |line| {
            line["clarp"] == "cut" && line["stage"] == "cancel"
        });
    });
    assert_eq!(
        kept_prompt(&place, "linestart"),
        clarp_acp::agy::steer::with_reply("1. Pho\n2. Ramen\n", "add-linestart")
    );
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ramen\n", "BANANA"]);
}

#[test]
fn a_message_that_comes_while_one_is_held_goes_after_it_in_order() {
    let (seen, place) = held_for("twice", |input, printed| {
        steer(input, "u-add2", "add-twice-2");
        printed.until("the second held after the first", |line| {
            line["clarp"] == "steer" && line["uuid"] == "u-add2" && line["why"] == "after a cut"
        });
    });
    assert_eq!(
        kept_prompt(&place, "twice"),
        clarp_acp::agy::steer::with_reply("1. Pho\n2. Ramen\n", "add-twice")
    );
    assert_eq!(kept_prompt(&place, "twice-2"), "add-twice-2");
    let reads: Vec<&str> = seen.iter().filter_map(read_of).collect();
    assert_eq!(reads, ["u-add", "u-add2"], "{seen:?}");
    assert_eq!(
        settled(&seen)[0]["user_message_uuids"],
        json!(["u-own", "u-add", "u-add2"])
    );
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men\n", "BANANA"]);
}

#[test]
fn a_message_answered_over_a_cancel_never_answered_is_shown_and_nothing_later_is_left_out() {
    let (seen, place) = held_for("skip", |_, _| {});
    // agy never answered the cancelled prompt: the message's answer passed over it. What agy
    // wrote after the message went was the message's, and follows its read.
    assert_eq!(pieces(&seen), ["1. Pho\n2. Ra", "men\n", "BANANA"]);
    assert_eq!(
        kept_prompt(&place, "skip"),
        clarp_acp::agy::steer::with_reply("1. Pho\n2. Ramen\n", "add-skip")
    );
    let read = printed_at(&seen, |line| read_of(line) == Some("u-add"));
    let banana = printed_at(&seen, |line| {
        line["params"]["update"]["content"]["text"] == "BANANA"
    });
    assert!(read < banana, "{seen:?}");
    let answer = &settled(&seen)[0];
    assert_eq!(answer["user_message_uuids"], json!(["u-own", "u-add"]));
    assert!(
        answer["result"].as_str().unwrap().ends_with("BANANA"),
        "{answer}"
    );
}
