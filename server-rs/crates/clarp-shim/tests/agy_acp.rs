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
        let (mut child, mut input) = place.open(&steering(&place, &format!("case-{mode}")), &[]);
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
    let first = printed.at(|line| {
        line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    });
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
    let (mut child, mut input) = place.open(&steering(&place, "case-sentence"), &[]);
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
    let thought = printed.at(|line| {
        line["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    });
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
    let (mut child, mut input) = place.open(&steering(&place, "case-closing"), &[]);
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
