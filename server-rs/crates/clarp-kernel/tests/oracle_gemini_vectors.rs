#![allow(clippy::unwrap_used, clippy::expect_used)]
use clarp_kernel::oracle::gemini::{Effect, Gemini, SETUP_SECONDS, healthy};
use serde_json::{Value, json};
#[test]
fn the_second_voice_matches_each_step_of_the_reference() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle-gemini-vectors.json")).unwrap();
    let mut count = 0;
    for story in fixture["stories"].as_array().unwrap() {
        let mut voice = Gemini::new(
            story["model"].as_str().unwrap().into(),
            story["voice"].as_str().unwrap().into(),
            1000.,
        );
        voice.languages = serde_json::from_value(story["languages"].clone()).unwrap();
        let mut now = 1000.;
        let mut ids = 0;
        let mut failed = None;
        let mut connection = 0;
        let mut connected = true;
        let mut broken = false;
        let mut hung_up = vec![];
        for step in story["steps"].as_array().unwrap() {
            count += 1;
            let context = format!("{} step {count}", story["name"]);
            let mut sent = vec![];
            let mut events = vec![];
            let mut flags = vec![];
            let mut error = false;
            let mut effects = vec![];
            match step["do"].as_str().unwrap() {
                "lost" => {
                    if !voice.began {
                        failed = Some(now);
                    }
                }
                "wait" => now += step["seconds"].as_f64().unwrap(),
                "interrupt" => voice.interrupt(),
                "routing" => voice.routing(step["id"].as_str().unwrap()),
                "finished" => effects = voice.finished(step["id"].as_str().unwrap()),
                "breaks" => broken = true,
                "send" => {
                    effects = voice.send(&step["event"], now);
                    if voice.closed && connected {
                        connected = false;
                        hung_up.push(connection);
                    }
                }
                "message" => {
                    let began = voice.began;
                    effects = voice.message(&step["message"], now, || {
                        ids += 1;
                        format!("{ids:032x}")
                    });
                    if voice.began && !began {
                        failed = None;
                    }
                }
                "resume" => {
                    if let Some(setup) = voice.resume_setup() {
                        if connected {
                            hung_up.push(connection);
                        }
                        connected = false;
                        let says = step["says"].as_array().unwrap();
                        if says.first() == Some(&json!("unreachable")) {
                            error = true;
                        } else {
                            connection += 1;
                            sent.push(json!({"on":connection,"message":setup}));
                            for v in says {
                                if v.get("setupComplete").is_some() {
                                    connected = true;
                                    break;
                                }
                                if v[0] == "wait" {
                                    now += v[1].as_f64().unwrap();
                                }
                            }
                            if connected {
                                events.push(voice.resumed(step["reason"].as_str().unwrap()));
                            } else {
                                error = true;
                            }
                        }
                    }
                    if !error {
                        assert_eq!(
                            step["resumed"],
                            connected && !events.is_empty(),
                            "{context}"
                        );
                    }
                }
                other => panic!("unknown action {other}"),
            }
            for effect in effects {
                let audio = matches!(effect, Effect::Audio(_));
                match effect {
                    Effect::Send(message) | Effect::Audio(message) => {
                        flags.push(audio);
                        if !connected {
                            continue;
                        }
                        if broken {
                            broken = false;
                            error = !(audio && voice.may_drop_audio());
                        } else {
                            sent.push(json!({"on":connection,"message":message}));
                        }
                    }
                    Effect::Event(event) => events.push(event),
                }
            }
            assert_eq!(json!(sent), step["sent"], "{context}");
            assert_eq!(json!(events), step["events"], "{context}");
            assert_eq!(json!(flags), step["asked_to_send"], "{context}");
            assert_eq!(step["began"], voice.began, "{context}");
            assert_eq!(step["closed"], voice.closed, "{context}");
            assert_eq!(step["connections"], connection + 1, "{context}");
            assert_eq!(step["hung_up"], json!(hung_up), "{context}");
            assert_eq!(step["due"], voice.due(now), "{context}");
            assert_eq!(step["taken_up_by"], json!(voice.handle()), "{context}");
            assert_eq!(step["used"], voice.usage(now), "{context}");
            assert_eq!(step["well"], healthy(now, failed), "{context}");
            assert_eq!(error, !step["raised"].is_null(), "{context}");
        }
    }
    for story in fixture["well"].as_array().unwrap() {
        let mut failed = None;
        for step in story.as_array().unwrap() {
            let at = step["at"].as_f64().unwrap();
            match step["do"].as_str() {
                Some("fails") => failed = Some(at),
                Some("begins") => failed = None,
                _ => {}
            }
            assert_eq!(step["well"], healthy(at, failed));
        }
    }
    assert_eq!(
        fixture["setup_within_seconds"].as_f64(),
        Some(SETUP_SECONDS as f64)
    );
    assert_eq!(count, 384);
}
