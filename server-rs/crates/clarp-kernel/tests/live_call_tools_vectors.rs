#![allow(clippy::unwrap_used)]
use clarp_kernel::live_call::receipts;
use serde_json::{Value, json};

#[test]
fn the_voice_is_told_exactly_what_the_director_confirmed() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let mut answers = 0;
    for row in fixture["outcomes"].as_array().unwrap() {
        assert_eq!(
            receipts::director(&row["answer"], row["status"].as_i64()),
            row["receipt"],
            "{row}"
        );
        answers += 1;
    }
    let mut notes = 0;
    for row in fixture["notes"].as_array().unwrap() {
        assert_eq!(
            json!(receipts::notes(row["receipts"].as_array().unwrap())),
            row["notes"],
            "note case {notes}"
        );
        notes += 1;
    }
    assert_eq!((answers, notes), (129, 102));
}
#[test]
fn tool_proposals_and_direct_prompts_follow_the_frozen_team() {
    use clarp_kernel::live_call::team::{self, Action, Prompt, Team};
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let members = [json!({"session":"test-member","name":"Theo"})];
    let mut steps = 0;
    for story in fixture["calls"].as_array().unwrap() {
        let mut team = Team::default();
        let mut nonce = 100;
        for step in story["steps"].as_array().unwrap() {
            let name = step["name"].as_str().unwrap();
            let p = team.propose(&members, name, &step["args"]);
            let mut effects = Vec::new();
            let answer = match p.action {
                Action::Answer(answer) => answer,
                Action::Delegate { normalized, .. } => {
                    let id = team::operation(
                        "rq-",
                        &["host-test", "call-one", p.session, &nonce.to_string()],
                    );
                    nonce += 1;
                    if story["delivery"] == "closed" {
                        team::ended(name)
                    } else {
                        effects.push(json!([
                            "prompt",
                            p.session,
                            "guard",
                            team::prompt("Guard", &normalized)
                        ]));
                        let outcome = match story["delivery"].as_str().unwrap() {
                            "sent" => Prompt::Sent,
                            "refused" => Prompt::Refused("stand-in refusal"),
                            "uncertain" => Prompt::Uncertain("stand-in uncertain"),
                            _ => unreachable!(),
                        };
                        team::prompted(p.name, &id, outcome)
                    }
                }
                _ if story["delivery"] == "closed" => team::ended(name),
                Action::Status => {
                    effects.push(json!(["status", p.session]));
                    team::read(name, p.name, json!({"state":"idle","last_said":"fixture"}))
                }
                Action::Transcript(limit) => {
                    effects.push(json!(["transcript", p.session, limit]));
                    team::read(
                        name,
                        p.name,
                        json!({"messages":[{"role":"assistant","text":"fixture"}]}),
                    )
                }
            };
            assert_eq!(answer, step["result"], "{name}");
            assert_eq!(json!(effects), step["effects"], "{name}");
            steps += 1;
        }
    }
    assert_eq!(steps, 140);
}

#[test]
fn a_callback_belongs_to_its_phone_and_its_one_due_owner() {
    use clarp_kernel::live_call::callback::{self, Binding, Director, Failure, Request};
    struct Regi<'a> {
        kind: &'a str,
        sent: Vec<Value>,
    }
    impl Director for Regi<'_> {
        fn save(&mut self, _: &Value) -> Result<(), Failure> {
            Ok(())
        }
        fn time(&self, epoch: &Value) -> String {
            if epoch == 3600 { "01:00" } else { "?" }.into()
        }
        fn post(&mut self, body: &Value) -> Result<Option<(u16, Value)>, Failure> {
            self.sent.push(body.clone());
            let first = self.sent.len() == 1;
            let cancelled = body["action"] == "cancel";
            let kind = self.kind;
            let status = match kind {
                "fired" => "fyrt",
                "firing" => "fyrer",
                "unclear-fired" => "fyrt_usikker",
                "cancelled-before" => "avlyst",
                "not-placed" => "ikke_plassert",
                "failed" => "feil",
                _ => "venter",
            };
            if kind == "uncertain" || kind == "uncertain-once" && first {
                return Ok(None);
            }
            let (code, reply) = match kind {
                "mismatch" if first => (
                    409,
                    json!({"error":"supersedes_mismatch","pending":"other-call"}),
                ),
                "refused" => (400, json!({"reason":"disabled"})),
                "server-error" => (500, json!({"error":"other"})),
                "invalid" => (200, json!({"ok":false,"result":"scheduled"})),
                _ => {
                    let result = match kind {
                        "already_scheduled" | "nothing_pending" | "unexpected-status" => kind,
                        "legacy-replace" if body["explicit"] == true => "already_scheduled",
                        _ if cancelled => "cancelled",
                        _ => "scheduled",
                    };
                    let mut reply = json!({"ok":true,"operation_id":body["operation_id"],"due":3600,"status":status,"result":result});
                    if kind == "unknown" {
                        reply["status"] = Value::Null;
                    }
                    if kind == "superseded" && body["explicit"] == true {
                        reply["superseded"] = json!("earlier");
                    }
                    (200, reply)
                }
            };
            Ok(Some((code, reply)))
        }
    }
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let binding = Binding {
        host: "host-test",
        principal: "phone-one",
        agent: "guard",
        call: "call-one",
    };
    let mut steps = 0;
    for story in fixture["callbacks"].as_array().unwrap() {
        let kind = story["kind"].as_str().unwrap();
        let config = json!({"callback":{"kind":if kind=="off"{"none"}else{"http"}},"callback_minutes":{"default":5,"min":1,"max":30}});
        let mut director = Regi {
            kind,
            sent: Vec::new(),
        };
        let mut ledger = Value::Null;
        for step in story["steps"].as_array().unwrap() {
            director.sent.clear();
            let request = Request {
                tool: step["tool"].as_str().unwrap(),
                args: &step["args"],
                config: &config,
                explicit: true,
                eligible: kind != "not-eligible",
            };
            let receipt = callback::execute(&binding, request, &mut ledger, &mut director)
                .ok()
                .unwrap();
            assert_eq!(json!(director.sent), step["effects"], "{kind}");
            assert_eq!(receipt, step["receipt"], "{kind}");
            steps += 1;
        }
    }
    assert_eq!(steps, 220);
}

#[test]
fn transcript_fragments_and_receipts_are_retained_across_intervening_events() {
    use clarp_kernel::live_call::transcript;
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let meta =
        json!({"call_id":"one","principal":"device-a","agent_session":"guard","agent":"Guard"});
    let mut runs = 0;
    for case in fixture["transcripts"].as_array().unwrap() {
        assert_eq!(
            json!(transcript::name(case["id"].as_str().unwrap())),
            case["name"]
        );
        assert_eq!(
            transcript::document(&meta, "T0", "T20", case["entries"].as_array().unwrap()),
            case["document"]
        );
        runs += 1;
    }
    assert_eq!(runs, 10);
}

#[test]
fn the_live_router_sees_only_new_user_requests_with_the_recent_conversation() {
    use clarp_kernel::live_call::routing::Routing;
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let story = &fixture["routes"][0];
    let snapshot = json!({"profile":{"display_name":"Guard"},"live_tools":story["config"]});
    let mut call = Routing::default();
    let mut count = 0;
    for step in story["steps"].as_array().unwrap() {
        let event = &step["event"];
        let at = step["at"].as_f64().unwrap();
        call.observe(event, at);
        let mut bodies = Vec::new();
        let mut spoken = Vec::new();
        if matches!(
            event["type"].as_str(),
            Some("tick" | "session.delegation.created")
        ) && let Some(route) = call.begin(&snapshot, at, || "12:13".into())
        {
            bodies.push(route.body.clone());
            spoken = call.finish(route, &[], false);
        }
        let expected: Vec<_> = step["body_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| &story["bodies"][n.as_u64().unwrap() as usize])
            .collect();
        assert_eq!(json!(bodies), json!(expected), "step {count}");
        assert_eq!(json!(spoken), step["spoken"], "step {count}");
        count += 1;
    }
    assert_eq!(count, 85);
}

#[test]
fn returned_answers_advance_only_on_delivery_and_replay_from_the_requested_part() {
    use clarp_kernel::live_call::answers::Answers;
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let mut count = 0;
    for case in fixture["answers"].as_array().unwrap() {
        let mut answers = Answers::default();
        answers
            .queue(
                "answer-one",
                "A sender",
                "a source",
                case["text"].as_str().unwrap(),
                "  a   question?  ",
            )
            .unwrap();
        for step in case["steps"].as_array().unwrap() {
            let read = &step["read"];
            let receipt = if read.is_null() {
                Value::Null
            } else {
                answers.read(read)
            };
            assert_eq!(receipt, step["receipt"]);
            let now = step["at"].as_f64().unwrap();
            let mut sent = Vec::new();
            let mut ack = Vec::new();
            if (read.is_null() || receipt["status"] != "nothing_to_read")
                && let Some(a) = answers.due(
                    now,
                    step["user"].as_f64().unwrap(),
                    step["voice"].as_f64().unwrap(),
                )
            {
                sent.push(json!([a.channel, a.text]));
                if answers.delivered(&a, step["delivery"].as_bool(), now) {
                    ack.push(a.id.clone());
                    answers.acknowledged(&a.id);
                }
            }
            assert_eq!(json!(sent), step["spoken"], "step {count}");
            assert_eq!(json!(ack), step["ack"], "step {count}");
            count += 1;
        }
    }
    assert_eq!(count, 100);
}
