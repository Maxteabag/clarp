//! The owner, on 24 Nov 2026: an answer is spoken only when voice is on in the app, and that on
//! every tool. A send that says nothing of `synthesize_audio` is answered in text, and its tool
//! is not told how to speak, a person's send too; one that asks for it is spoken and told.
//! Messages read together, and a message that steers a running turn, follow what each asked.

use super::*;

/// Claude Code, as far as these turns need it. Each message it is handed is kept, with what
/// the Host says after the user's words, as one JSON pair a line in `handed`. A message that
/// begins with `hold` starts a tool and waits for the next message, which steers the turn: it
/// is echoed and answered. Any other message is answered with its own words.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, pathlib, sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)
root = pathlib.Path(__file__).resolve().parent
replay = "--replay-user-messages" in sys.argv
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def handed():
    record = json.loads(sys.stdin.readline())
    content = record["message"]["content"]
    texts = [block.get("text", "") for block in content] + [""]
    with (root / "handed").open("a") as kept:
        kept.write(json.dumps(texts[:2]) + "\n")
    return record["uuid"], texts[0]

def echo(uid):
    if replay:
        say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})

def answer(uid, words, *uids):
    say("assistant", message={"id": "msg_" + uid, "role": "assistant",
        "content": [{"type": "text", "text": words}]})
    say("result", subtype="success", is_error=False, result=words, user_message_uuids=uids)

first, text = handed()
say("system", subtype="init")
echo(first)
if not text.startswith("hold"):
    answer(first, text, first)
    raise SystemExit(0)
tool = "tool-" + first
say("stream_event", event={"type": "content_block_start", "index": 0,
    "content_block": {"type": "tool_use", "id": tool, "name": "Bash", "input": {}}})
say("assistant", message={"id": "msg_call_" + first, "role": "assistant",
    "content": [{"type": "tool_use", "id": tool, "name": "Bash", "input": {}}]})
uid, follow = handed()
say("user", message={"content": [{"type": "tool_result", "tool_use_id": tool, "content": "done"}]})
echo(uid)
answer(uid, follow, first, uid)
"#;

/// Antigravity, which is told how to speak in the words before the prompt. Every prompt it is
/// given is kept, one JSON string a line, in `prompts`. A prompt that ends with `hold` waits
/// for the file `release`.
const AGY: &str = r#"#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
prompt = next((a[len("--print="):] for a in args if a.startswith("--print=")), "")
with (root / "prompts").open("a") as kept:
    kept.write(json.dumps(prompt) + "\n")
number = sum(1 for _ in (root / "prompts").open())
conversation = "11111111-2222-3333-4444-555555555555"
def say(event, **body):
    print(json.dumps({"event": event, **body}), flush=True)
def step(index, state, kind, **more):
    update = {"conversation_id": conversation, "step_index": index, "state": state,
              "step_type": kind, **more}
    say("step_update", step_update=update)
say("init", conversation_id=conversation,
    init={"model": "gemini-stand-in-low", "cwd": os.getcwd(), "tools": []})
step(0, "DONE", "user_input", text_delta=prompt)
if prompt.endswith("hold"):
    (root / "holding").touch()
    end = time.monotonic() + 60
    while not (root / "release").exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(0.05)
answer = "agy answer %d" % number
step(1, "DONE", "agent_response", text_delta=answer)
say("result", result={"conversation_id": conversation, "status": "SUCCESS", "response": answer})
"#;

#[test]
fn agy_voice_keeps_long_then_short_in_order_without_result_replay() {
    let place = Place::new("agy-cumulative-speech");
    let agy = place.root.join("agy");
    let conversation = "11111111-2222-3333-4444-555555555555";
    let long = format!(
        "<speak>First line. {}</speak>",
        "The longer opening stays ahead of the short answer. ".repeat(8)
    );
    let short = "<speak>First summary.</speak>";
    let next = "<speak>Next turn only.</speak>";
    let init = json!({"event":"init","conversation_id":conversation});
    let step = |index, said: &str| {
        json!({"event":"step_update","step_update":{"conversation_id":conversation,
            "step_type":"agent_response","step_index":index,"state":"DONE","text_delta":said}})
    };
    let result = |said: &str| {
        json!({"event":"result","result":{"conversation_id":conversation,
            "status":"SUCCESS","response":said}})
    };
    program(
        &agy,
        &format!(
            "#!/bin/sh\ncase \"$*\" in\n*voice-fixture-next*)\ncat <<'NEXT'\n{init}\n{}\nNEXT\n;;\n*)\ncat <<'FIRST'\n{init}\n{}\n{}\n{}\nFIRST\n;;\nesac\n",
            result(next),
            step(1, &long),
            step(2, short),
            result(&format!("{long}{short}")),
        ),
    );
    let voice = cartesia_stand_in(vec![23; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n",
    )
    .unwrap();
    let host = place.start_in(
        "",
        &["--speech-keys", owners.to_str().unwrap()],
        &[
            ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
            ("CLARP_CARTESIA_ENDPOINT", &voice.address),
        ],
    );
    let made = host.post(
        "/agents",
        json!({"name":"AGY voice test","backend":"agy","voice_id":"{\"cartesia\":\"fixture\"}"}),
    );
    let session = made["session"].as_str().unwrap();
    host.post("/select", json!({"session": session}));
    let heard = host.listen();
    wait_for("the event stream to answer", || {
        !heard.lock().unwrap().is_empty()
    });
    let clips = || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["type"] == "audio" && event["session"] == session)
            .cloned()
            .collect::<Vec<_>>()
    };
    let requested = || {
        voice
            .asked()
            .iter()
            .filter_map(|req| req["transcript"].as_str().map(str::to_owned))
            .collect::<Vec<_>>()
    };
    voice.held.store(true, Ordering::SeqCst);
    let first = host.send_aloud(session, "voice-fixture-first", "first-voice");
    wait_for("the long clip to announce and begin", || {
        requested()
            .first()
            .is_some_and(|said| said.contains("First line."))
            && clips().len() == 1
    });
    let second = host.send_aloud(session, "voice-fixture-next", "second-voice");
    wait_for("the next turn's terminal-only answer", || {
        host.turns(session).iter().any(|row| {
            row["role"] == "assistant"
                && row["text"]
                    .as_str()
                    .is_some_and(|said| said.contains("Next turn only."))
        })
    });
    let held = clips();
    voice.held.store(false, Ordering::SeqCst);
    assert_eq!(
        held.len(),
        1,
        "a later line announced while the first was still making"
    );

    let replies: Vec<_> = host
        .turns(session)
        .into_iter()
        .filter(|row| row["role"] == "assistant")
        .collect();
    assert_eq!(
        replies.len(),
        3,
        "the cumulative result must not write a second copy of the first turn"
    );
    wait_for("three clips, in the original speech order", || {
        requested().len() >= 3 && clips().len() >= 3
    });
    let requested = requested();
    assert_eq!(requested.len(), 3);
    for (actual, marker) in
        requested
            .iter()
            .zip(["First line.", "First summary.", "Next turn only."])
    {
        assert!(
            actual.starts_with(marker),
            "unexpected synthesis order: {requested:?}"
        );
    }
    let clips = clips();
    assert_eq!(clips.len(), 3);
    assert_eq!(clips[0]["trace_id"], first["trace_id"]);
    assert_eq!(clips[1]["trace_id"], first["trace_id"]);
    assert_eq!(clips[2]["trace_id"], second["trace_id"]);
    assert_ne!(first["trace_id"], second["trace_id"]);
    assert!(
        clips
            .windows(2)
            .all(|pair| pair[0]["event_id"].as_u64().unwrap()
                < pair[1]["event_id"].as_u64().unwrap())
    );
}

#[test]
fn a_send_is_spoken_and_its_tool_told_how_to_speak_only_when_it_asks() {
    let place = Place::new("spoken-when-asked");
    let (claude, agy) = (place.root.join("claude"), place.root.join("agy"));
    program(&claude, CLAUDE);
    program(&agy, AGY);
    let voice = cartesia_stand_in(vec![23; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n\
         model='sonic-3'\n",
    )
    .unwrap();
    let home = place.root.join("home");
    let xdg = [".config", ".local/share", ".cache", ".local/state"].map(|name| home.join(name));
    let nowhere = place.root.join("absent");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("XDG_CONFIG_HOME", xdg[0].to_str().unwrap()),
        ("XDG_DATA_HOME", xdg[1].to_str().unwrap()),
        ("XDG_CACHE_HOME", xdg[2].to_str().unwrap()),
        ("XDG_STATE_HOME", xdg[3].to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_GROK_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_MUSE_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_OPENCODE_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_CARTESIA_ENDPOINT", voice.address.as_str()),
        ("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ("GEMINI_API_KEY", ""),
        ("DEEPGRAM_API_KEY", ""),
        ("GOOGLE_VERTEX_API_KEY", ""),
    ];
    let args = ["--speech-keys", owners.to_str().unwrap()];
    // With a token of its own, so that a paired phone is a device of its own that can call.
    let host = place.start_in("admin", &args, &world);
    // Names of no persona the Host has built in: who the agent is says nothing of speech.
    let make = |name: &str, backend: &str| {
        let own = json!({ "cartesia": format!("fixture-voice-{backend}") }).to_string();
        let made = host.post(
            "/agents",
            json!({ "name": name, "backend": backend, "voice_id": own }),
        );
        made["session"].as_str().unwrap().to_string()
    };
    let lines = |name: &str| -> Vec<Value> {
        std::fs::read_to_string(place.root.join(name)).map_or_else(
            |_| Vec::new(),
            |kept| {
                kept.lines()
                    .map(|line| serde_json::from_str(line).unwrap())
                    .collect()
            },
        )
    };
    let spoken = |words: &str| {
        voice.asked().iter().any(|asked| {
            asked["transcript"]
                .as_str()
                .is_some_and(|said| said.contains(words))
        })
    };
    let answered = |session: &str, id: &str| {
        let mine = format!("u-{id}");
        wait_for(&format!("the answer to {id}"), || {
            let rows = host.turns(session);
            let at = rows.iter().position(|row| row["id"] == mine);
            at.is_some_and(|at| rows[at + 1..].iter().any(|row| row["role"] == "assistant"))
                && host.state(session) == "done"
        });
    };

    // Claude is told how to speak beside the user's words, by the Host's reminder. A person's
    // send that says nothing is answered in text, and its reminder says nothing of speech.
    let reader = make("Fixture Reader", "claude");
    host.send(&reader, "<speak>Plain words.</speak>", "c1");
    answered(&reader, "c1");
    let handed = lines("handed");
    let reminder = handed[0][1].as_str().unwrap();
    assert!(
        reminder.contains("Fixture Reader") && !reminder.contains("<speak>"),
        "{reminder}"
    );
    // Asked for, it is spoken and told.
    host.send_aloud(&reader, "<speak>Asked words.</speak>", "c2");
    answered(&reader, "c2");
    let handed = lines("handed");
    let reminder = handed[1][1].as_str().unwrap();
    assert!(reminder.contains("<speak>"), "{reminder}");
    wait_for("the asked words spoken", || spoken("Asked words."));
    // Said false, it is not.
    host.post(
        "/send",
        json!({
            "session": reader, "text": "<speak>Refused words.</speak>",
            "client_msg_id": "c3", "synthesize_audio": false,
        }),
    );
    answered(&reader, "c3");
    assert!(!lines("handed")[2][1].as_str().unwrap().contains("<speak>"));

    // A message of the user's that steers a running turn carries the app's audio setting
    // now: the turn is spoken as it asks, whatever the turn's first message asked. One that
    // asks for speech, read into a turn whose tool was not told how to speak, is given the
    // Host's reminder with it, as the turn would have been at its launch: the second block
    // the tool reads with it, whatever the tool answers.
    let steer = |id: &str, first_aloud: bool, follow: &str, aloud: bool| -> String {
        let first = format!("{id}-first");
        if first_aloud {
            host.send_aloud(&reader, "hold", &first);
        } else {
            host.send(&reader, "hold", &first);
        }
        wait_for("the tool in the live projection", || {
            host.state(&reader) == "tool"
        });
        let mut body = json!({ "session": reader, "text": follow, "client_msg_id": id });
        if aloud {
            body["synthesize_audio"] = json!(true);
        }
        // It waits as a queue item until the running tool reads it. The held turn ends only
        // by reading it, so its answer is the proof that it steered that turn.
        host.post("/send", body);
        answered(&reader, id);
        let handed = lines("handed");
        let last = &handed[handed.len() - 1];
        assert_eq!(last[0], follow, "the follow-up was handed to the held turn");
        last[1].as_str().unwrap().to_string()
    };
    // Voice turned on since the turn began: told how to speak, and spoken.
    let told = steer("s1", false, "<speak>Steered aloud.</speak>", true);
    assert!(
        told.contains("<speak>") && told.contains("Fixture Reader"),
        "{told}"
    );
    wait_for("the steered words spoken", || spoken("Steered aloud."));
    // Voice off all along: nothing told, only read.
    let told = steer("s2", false, "<speak>Steered quietly.</speak>", false);
    assert_eq!(told, "");
    // Voice turned off since the turn began: the newest message decides, and it is read.
    let told = steer("s3", true, "<speak>Voice turned off.</speak>", false);
    assert_eq!(told, "");
    // Voice on all along: the tool was told at the launch, and is not told again.
    let told = steer("s4", true, "<speak>Still aloud.</speak>", true);
    assert_eq!(told, "");
    wait_for("the words of a turn spoken all along", || {
        spoken("Still aloud.")
    });
    std::thread::sleep(Duration::from_secs(2));
    assert!(!spoken("Steered quietly.") && !spoken("Voice turned off."));
    assert!(!spoken("Plain words.") && !spoken("Refused words."));

    // A call's turn is speech by nature: a message typed to the agent while it runs, which
    // asks for no speech, is read into it and does not turn it off.
    let (phone, _) = pair(&host, "Phone", "full");
    let (status, call) = host.ask_as(
        &phone,
        "POST",
        "/calls",
        Some(&json!({ "agents": [reader] })),
    );
    assert!(status < 300, "{status} {call}");
    let call_id = call["call"]["call_id"].as_str().unwrap().to_string();
    let (status, said) = host.ask_as(
        &phone,
        "POST",
        "/send",
        Some(&json!({
            "session": reader, "text": "hold", "client_msg_id": "c-first", "call_id": call_id,
        })),
    );
    assert_eq!(status, 200, "{said}");
    wait_for("the call's turn in its tool", || {
        host.state(&reader) == "tool"
    });
    host.post(
        "/send",
        json!({
            "session": reader, "text": "<speak>Typed during the call.</speak>",
            "client_msg_id": "c-typed", "synthesize_audio": false,
        }),
    );
    answered(&reader, "c-typed");
    wait_for("the call's turn still spoken", || {
        spoken("Typed during the call.")
    });
    host.ask_as(
        &phone,
        "POST",
        "/calls/end",
        Some(&json!({ "call_id": call_id })),
    );

    // Antigravity is told how to speak in the words before the prompt: only when it is asked.
    let gatherer = make("Fixture Gatherer", "agy");
    let prompts = || -> Vec<String> {
        lines("prompts")
            .into_iter()
            .map(|prompt| prompt.as_str().unwrap().to_string())
            .collect()
    };
    let turned = |count: usize| {
        wait_for("the tool's turn", || {
            prompts().len() == count && host.state(&gatherer) == "done"
        });
    };
    host.send(&gatherer, "plain words", "a1");
    turned(1);
    assert!(!prompts()[0].contains("<speak>"), "{:?}", prompts());
    host.send_aloud(&gatherer, "asked words", "a2");
    turned(2);
    assert!(prompts()[1].contains("<speak>"), "{:?}", prompts());

    // The user's messages that wait behind a turn go to the tool in one prompt. Each carries
    // the app's audio setting when it was sent: the newest of them decides.
    let gather = |round: usize, said: [(&str, &str, bool); 2]| -> String {
        let (holding, release) = (place.root.join("holding"), place.root.join("release"));
        let _ = std::fs::remove_file(&holding);
        let _ = std::fs::remove_file(&release);
        host.send(&gatherer, "hold", &format!("hold-{round}"));
        wait_for("the turn to hold", || holding.exists());
        for (id, text, aloud) in said {
            let sent = if aloud {
                host.send_aloud(&gatherer, text, id)
            } else {
                host.send(&gatherer, text, id)
            };
            assert_eq!(sent["queued"], true, "{sent}");
        }
        // The held turn's prompt is in, and the one for the two comes after the release.
        let count = prompts().len() + 1;
        std::fs::write(&release, "").unwrap();
        turned(count);
        let joined = prompts()[count - 1].clone();
        let together = clarp_kernel::told::together(&[said[0].1, said[1].1]);
        assert!(joined.contains(&together), "{joined}");
        assert!(
            !prompts()[count - 2].contains("<speak>"),
            "the held turn asked nothing"
        );
        joined
    };
    let joined = gather(
        1,
        [("j1", "unsaid words", false), ("j2", "asked words", true)],
    );
    assert!(joined.contains("<speak>"), "{joined}");
    let joined = gather(
        2,
        [("j3", "first quiet", false), ("j4", "second quiet", false)],
    );
    assert!(!joined.contains("<speak>"), "{joined}");
    // Voice turned off between the two: the newest says the turn is read, not spoken.
    let joined = gather(
        3,
        [("j5", "asked first", true), ("j6", "then quiet", false)],
    );
    assert!(!joined.contains("<speak>"), "{joined}");
}

/// What an app says of its audio setting where a message is changed or an answer is given:
/// a reword and a Send now replace the waiting message's own flag with the one they carry and
/// keep it when they carry none; an answer to a request is spoken only when it asks.
#[test]
fn a_reword_a_send_now_and_an_answer_carry_the_apps_audio_setting() {
    let place = Place::new("spoken-when-changed");
    let voice = cartesia_stand_in(vec![29; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n\
         model='sonic-3'\n",
    )
    .unwrap();
    let world = [
        ("CLARP_CARTESIA_ENDPOINT", voice.address.as_str()),
        ("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ("GEMINI_API_KEY", ""),
        ("DEEPGRAM_API_KEY", ""),
        ("GOOGLE_VERTEX_API_KEY", ""),
    ];
    let args = ["--speech-keys", owners.to_str().unwrap()];
    let host = place.start_in("", &args, &world);
    let own = json!({ "cartesia": "fixture-voice-changed" }).to_string();
    let made = host.post(
        "/agents",
        json!({ "name": "Fixture Changer", "backend": "scripted", "voice_id": own }),
    );
    let agent = made["session"].as_str().unwrap().to_string();
    let spoken = |words: &str| {
        voice.asked().iter().any(|asked| {
            asked["transcript"]
                .as_str()
                .is_some_and(|said| said.contains(words))
        })
    };
    let answered = |id: &str| {
        let mine = format!("u-{id}");
        wait_for(&format!("the answer to {id}"), || {
            let rows = host.turns(&agent);
            let at = rows.iter().position(|row| row["id"] == mine);
            at.is_some_and(|at| rows[at + 1..].iter().any(|row| row["role"] == "assistant"))
                && host.state(&agent) == "done"
        });
    };
    let flag = |id: &str| -> Value {
        let queue = host.get(&format!("/turn-queue?session={agent}"));
        let items = queue["items"].as_array().unwrap();
        let item = items.iter().find(|item| item["id"] == id);
        item.unwrap_or_else(|| panic!("{id} waits: {queue}"))["synthesize_audio"].clone()
    };

    // A Stop holds the queue, and three messages sent at a set time that ask for the queue
    // wait in it until it is resumed, each as it was sent.
    host.post("/stop", json!({ "session": agent }));
    let held = |id: &str, text: &str, aloud: Option<bool>| {
        let mut body = json!({
            "session": agent, "text": text, "client_msg_id": id,
            "origin": "schedule", "queue_if_busy": true,
        });
        if let Some(aloud) = aloud {
            body["synthesize_audio"] = json!(aloud);
        }
        let sent = host.post("/send", body);
        assert_eq!(sent["queued"], true, "{sent}");
    };
    held("w1", "<speak>First waiting.</speak>", None);
    held("w2", "<speak>Second waiting.</speak>", Some(true));
    held("w3", "<speak>Third waiting.</speak>", Some(true));
    assert_eq!(
        (flag("w1"), flag("w2"), flag("w3")),
        (json!(false), json!(true), json!(true))
    );

    // A reword with the flag replaces it; one without keeps it; one that is no bool is refused.
    let reword = |id: &str, body: Value| host.ask("PUT", &format!("/turn-queue/{id}"), Some(&body));
    let said = reword(
        "w1",
        json!({ "text": "<speak>Sent now aloud.</speak>", "synthesize_audio": true }),
    );
    assert_eq!(said.0, 200, "{said:?}");
    assert_eq!(flag("w1"), true);
    assert_eq!(
        reword("w2", json!({ "text": "<speak>Sent now quietly.</speak>" })).0,
        200
    );
    assert_eq!(flag("w2"), true);
    let refused = reword("w2", json!({ "text": "x", "synthesize_audio": "yes" }));
    assert_eq!(refused.0, 400, "{refused:?}");
    assert_eq!(flag("w2"), true);

    // Each is sent by hand.
    let send_now = |id: &str, body: Option<Value>| {
        host.ask("POST", &format!("/turn-queue/{id}/send"), body.as_ref())
    };
    // Sent now as it was reworded: spoken.
    let sent = send_now("w1", None);
    assert_eq!(sent.0, 202, "{sent:?}");
    answered("w1");
    wait_for("the reworded words spoken", || spoken("Sent now aloud."));
    // Sent now with the flag off, though it was sent to be spoken: not spoken.
    let sent = send_now("w2", Some(json!({ "synthesize_audio": false })));
    assert_eq!(sent.0, 202, "{sent:?}");
    answered("w2");
    // Sent now with nothing said: as it was sent, spoken.
    assert_eq!(send_now("w3", Some(json!({}))).0, 202);
    answered("w3");
    wait_for("the third spoken", || spoken("Third waiting."));
    assert!(!spoken("Sent now quietly."));

    // An answer to a request is spoken only when the app that answers asks for it. While a
    // turn runs, each answer waits in the queue and says so there.
    host.post("/turn-queue/resume", json!({ "session": agent }));
    host.post(
        "/send",
        json!({ "session": agent, "text": "sleep 30", "client_msg_id": "busy" }),
    );
    wait_for("the turn to run", || host.state(&agent) != "done");
    let ask = |question: &str| -> String {
        let (status, made) = host.ask(
            "POST",
            "/decisions",
            Some(&json!({ "session": agent, "title": "Approval", "question": question })),
        );
        assert_eq!(status, 201, "{made}");
        made["artifact"]["decision"]["decision_id"]
            .as_str()
            .unwrap()
            .to_string()
    };
    let resolve = |id: &str, more: Value| {
        let mut body = json!({ "choice": "accepted", "expected_revision": 1 });
        body.as_object_mut()
            .unwrap()
            .extend(more.as_object().unwrap().clone());
        host.ask("POST", &format!("/decisions/{id}/resolve"), Some(&body))
    };
    let waiting = |id: &str| {
        let queue = host.get(&format!("/turn-queue?session={agent}"));
        let item = format!("decision-{id}");
        queue["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["id"] == item)
    };
    let quiet = ask("Proceed quietly?");
    let refused = resolve(&quiet, json!({ "synthesize_audio": 1 }));
    assert_eq!(refused.0, 400, "{refused:?}");
    // `null` is no wish, as on the queue's routes.
    let said = resolve(&quiet, json!({ "synthesize_audio": null }));
    assert_eq!(said.0, 200, "{said:?}");
    let aloud = ask("Proceed aloud?");
    assert_eq!(resolve(&aloud, json!({ "synthesize_audio": true })).0, 200);
    wait_for("both answers in the queue", || {
        waiting(&quiet) && waiting(&aloud)
    });
    assert_eq!(flag(&format!("decision-{quiet}")), false);
    assert_eq!(flag(&format!("decision-{aloud}")), true);
    // A question answered in words takes it beside its answer as well.
    let (status, made) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": agent, "title": "Words", "question": "Which words?",
            "response_type": "text_input",
        })),
    );
    assert_eq!(status, 201, "{made}");
    let worded = made["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let (status, said) = host.ask(
        "POST",
        &format!("/decisions/{worded}/resolve"),
        Some(&json!({
            "answer": { "text": "These words" }, "expected_revision": 1,
            "synthesize_audio": true,
        })),
    );
    assert_eq!(status, 200, "{said}");
    wait_for("the worded answer in the queue", || waiting(&worded));
    assert_eq!(flag(&format!("decision-{worded}")), true);

    // A Send now refused because the turn still runs has already given the item the app's
    // setting, and says so to the apps. A body that is not JSON is refused before anything.
    let heard = host.listen();
    let sent = host.send(&agent, "waits for its turn", "w4");
    assert_eq!(sent["queued"], true, "{sent}");
    assert_eq!(flag("w4"), false);
    wait_for("the apps told of the item waiting", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "queue-updated" && event["client_msg_id"] == "w4")
    });
    let garbled = fetch(
        &host,
        "",
        "POST",
        "/turn-queue/w4/send",
        &[("Content-Type", "application/json")],
        b"{\"synthesize_audio\": tru",
    );
    assert_eq!(garbled.status, 400);
    assert_eq!(flag("w4"), false);
    let told_before = heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| event["type"] == "queue-updated")
        .count();
    let refused = host.ask(
        "POST",
        "/turn-queue/w4/send",
        Some(&json!({ "synthesize_audio": true })),
    );
    assert_eq!(refused.0, 409, "{refused:?}");
    assert_eq!(flag("w4"), true);
    wait_for("the apps told of the changed item", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["type"] == "queue-updated")
            .count()
            > told_before
    });
}

/// Antigravity, as `AGY` above, but each answer marks itself for the ear, and a prompt that
/// ends with `hold-<tag>` waits for the file `release-<tag>`: the same file however often the
/// prompt is given, so a turn tried again after a restart waits the same way.
const AGY_SPOKEN: &str = r#"#!/usr/bin/env python3
import json, os, pathlib, re, sys, time
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
prompt = next((a[len("--print="):] for a in args if a.startswith("--print=")), "")
with (root / "prompts").open("a") as kept:
    kept.write(json.dumps(prompt) + "\n")
conversation = "11111111-2222-3333-4444-555555555555"
def say(event, **body):
    print(json.dumps({"event": event, **body}), flush=True)
def step(index, state, kind, **more):
    update = {"conversation_id": conversation, "step_index": index, "state": state,
              "step_type": kind, **more}
    say("step_update", step_update=update)
say("init", conversation_id=conversation,
    init={"model": "gemini-stand-in-low", "cwd": os.getcwd(), "tools": []})
step(0, "DONE", "user_input", text_delta=prompt)
held = re.search(r"hold-(\w+)$", prompt)
tag = held.group(1) if held else "none"
if held:
    (root / ("holding-" + tag)).touch()
    end = time.monotonic() + 90
    while not (root / ("release-" + tag)).exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(0.05)
answer = "<speak>Answer %s.</speak>" % tag
step(1, "DONE", "agent_response", text_delta=answer)
say("result", result={"conversation_id": conversation, "status": "SUCCESS", "response": answer})
"#;

/// The user's messages read together into one turn are spoken as the newest of them asked,
/// also when the Host starts again while that turn runs: it is taken up as it began, not as
/// its first message said.
#[test]
fn a_turn_read_together_keeps_its_newest_flag_across_a_restart() {
    let place = Place::new("spoken-after-restart");
    let agy = place.root.join("agy");
    program(&agy, AGY_SPOKEN);
    let voice = cartesia_stand_in(vec![31; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n\
         model='sonic-3'\n",
    )
    .unwrap();
    let nowhere = place.root.join("absent");
    let world = [
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_CARTESIA_ENDPOINT", voice.address.as_str()),
        ("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ("GEMINI_API_KEY", ""),
        ("DEEPGRAM_API_KEY", ""),
        ("GOOGLE_VERTEX_API_KEY", ""),
    ];
    let args = ["--speech-keys", owners.to_str().unwrap()];
    let mut host = place.start_in("", &args, &world);
    let own = json!({ "cartesia": "fixture-voice-restart" }).to_string();
    let made = host.post(
        "/agents",
        json!({ "name": "Fixture Restarted", "backend": "agy", "voice_id": own }),
    );
    let agent = made["session"].as_str().unwrap().to_string();
    let spoken = |words: &str| {
        voice.asked().iter().any(|asked| {
            asked["transcript"]
                .as_str()
                .is_some_and(|said| said.contains(words))
        })
    };
    let mark = |name: &str| std::fs::write(place.root.join(name), "").unwrap();
    let marked = |name: &str| place.root.join(name).exists();
    let send = |host: &Host, id: &str, text: &str, aloud: bool| {
        let body = json!({
            "session": agent, "text": text, "client_msg_id": id, "synthesize_audio": aloud,
        });
        host.post("/send", body)
    };

    // Each round: a turn holds, two of the user's messages wait behind it and are read
    // together into the next turn, which holds in turn; the Host starts again while it does,
    // and then it answers.
    for (round, first, second, aloud) in [("a", true, false, false), ("b", false, true, true)] {
        send(
            &host,
            &format!("busy-{round}"),
            &format!("hold-busy{round}"),
            false,
        );
        wait_for("the first turn to hold", || {
            marked(&format!("holding-busy{round}"))
        });
        let sent = send(&host, &format!("{round}1"), "the first of two", first);
        assert_eq!(sent["queued"], true, "{sent}");
        let sent = send(
            &host,
            &format!("{round}2"),
            &format!("hold-{round}"),
            second,
        );
        assert_eq!(sent["queued"], true, "{sent}");
        mark(&format!("release-busy{round}"));
        wait_for("the turn read together to hold", || {
            marked(&format!("holding-{round}"))
        });
        host.kill();
        host = place.start_in("", &args, &world);
        mark(&format!("release-{round}"));
        let words = format!("Answer {round}.");
        if aloud {
            wait_for("the answer spoken", || spoken(&words));
        } else {
            wait_for("the answer written", || {
                host.conversation(&agent)
                    .iter()
                    .any(|(role, text)| role == "assistant" && text.contains(&words))
            });
            std::thread::sleep(Duration::from_secs(2));
            assert!(!spoken(&words), "round {round}: the newest said text");
        }
        wait_for("the agent to be done", || host.state(&agent) == "done");
        // The turn was taken up where it ran, not launched again: its tool was given the
        // prompt read together once. A second launch would start from its messages anew and
        // pass without the Host having taken up the running turn.
        let kept = std::fs::read_to_string(place.root.join("prompts")).unwrap();
        let held = format!("hold-{round}");
        let launches = kept
            .lines()
            .map(|line| serde_json::from_str::<String>(line).unwrap())
            .filter(|prompt| prompt.ends_with(&held))
            .count();
        assert_eq!(
            launches, 1,
            "round {round}: one launch of the turn read together"
        );
    }
}
