//! What an app is answered when it asks for the use of tools to be explained is what the
//! Python Host's own service answers.
//!
//! `tests/fixtures/tool-explanation-service-vectors.json` (written by
//! `tools/tool-explanation-service-vectors.py`) holds scenes that were run against that
//! service, with its queue, its worker, its table and its ledger, and a stand-in for the
//! model. Here the same scenes are played to the desk: every request is answered alike (or
//! refused in the same words), the model is asked about the same parts, the same rows are
//! learned and the ledger counts the same. With them: the words a model is asked with, and
//! what the ledger says of made-up days.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::explain::desk::{self, Asked, Desk, Janitor, Pace, Said};
use clarp_kernel::explain::learned::{PROMPT_VERSION, Row};
use clarp_kernel::explain::read;
use serde_json::{Map, Value, json};

const VECTORS: &str = include_str!("fixtures/tool-explanation-service-vectors.json");

fn vectors() -> Value {
    serde_json::from_str(VECTORS).unwrap()
}

/// What the stand-in for the model says of one request: the rule of the tool that wrote
/// the vectors.
fn answer(request: &Value, level: u8) -> Value {
    let activity = &request["activity"];
    let of = |key: &str| activity[key].as_str().filter(|said| !said.is_empty());
    let command = of("command")
        .or(of("name"))
        .or(of("kind"))
        .unwrap_or_default();
    let name = command.split_whitespace().next().unwrap_or("nothing");
    let slots = request["slots"].as_object().cloned().unwrap_or_default();
    let known = ["path1_name", "path1", "text1", "num1"];
    let place = known
        .iter()
        .find(|key| slots.contains_key(**key))
        .map(|key| (*key).to_string());
    let place = place.or_else(|| slots.keys().next().cloned());
    let value = place
        .as_ref()
        .and_then(|place| slots[place].as_str())
        .unwrap_or_default();
    let reader = if level == 2 {
        String::new()
    } else {
        format!(" (for reader {level})")
    };
    let said =
        |sentence: String, template: String| json!({ "text": sentence, "template": template });
    match (name, &place) {
        ("tpl-good", Some(place)) => said(
            format!("Works on {value} as asked{reader}."),
            format!("Works on {{{place}}} as asked{reader}."),
        ),
        ("tpl-literal", Some(_)) => {
            let sentence = format!("Works on {value} as asked.");
            said(sentence.clone(), sentence)
        }
        ("tpl-mismatch", Some(place)) => said(
            "Does something else.".to_string(),
            format!("Works on {{{place}}} as asked."),
        ),
        ("tpl-unknown", _) => said("Works on it.".to_string(), "Works on {path9}.".to_string()),
        ("tpl-number", Some(place)) => said(
            format!("Tries {value} 3 times."),
            format!("Tries {{{place}}} 3 times."),
        ),
        _ => said(format!("Runs {name}{reader}."), String::new()),
    }
}

/// What the stand-in does with a batch, by how the scene says it behaved.
fn model(how: &str, level: u8, requests: &[Value]) -> Said {
    let by_number = |requests: &[Value], bare: bool| {
        let rows = requests.iter().map(|request| {
            let said = answer(request, level);
            let said = if bare { said["text"].clone() } else { said };
            (request["id"].as_str().unwrap().to_string(), said)
        });
        Value::Object(rows.collect())
    };
    match how {
        "fails" => Said::Failed("translator_failed"),
        "late" => Said::Failed("timeout"),
        "gone" => Said::Failed("codex_unavailable"),
        "unusable" => Said::Answered(json!(["not", "a", "table"])),
        "short" => Said::Answered(by_number(&requests[..requests.len() - 1], false)),
        "long" => {
            let rows = requests.iter().map(|request| {
                let said = json!({ "text": "x".repeat(241), "template": "" });
                (request["id"].as_str().unwrap().to_string(), said)
            });
            Said::Answered(Value::Object(rows.collect()))
        }
        "words" => Said::Answered(by_number(requests, true)),
        _ => Said::Answered(by_number(requests, false)),
    }
}

/// What a model was asked about, whatever number each thing had and in whatever order.
fn about(requests: &[Value]) -> Vec<String> {
    let mut about: Vec<String> = requests
        .iter()
        .map(|request| {
            let mut request = request.as_object().unwrap().clone();
            request.remove("id");
            let fields: BTreeMap<String, Value> = request.into_iter().collect();
            serde_json::to_string(&fields).unwrap()
        })
        .collect();
    about.sort();
    about
}

/// What was read of the scripts a part names, of the files a scene has in its agent's
/// directory: what is there, by a plain name, and not hidden; each once, two at most.
fn scripts(files: &BTreeMap<String, String>, activity: &Map<String, Value>) -> Vec<Value> {
    let mut read_of = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for name in read::script_names(activity) {
        // A script that is named twice (in the command and in what the tool was given) is
        // read once.
        let hidden = name.split('/').any(|part| part.starts_with('.'));
        let Some(holds) = files
            .get(&name)
            .filter(|_| !hidden && !seen.contains(&name))
        else {
            continue;
        };
        seen.push(name.clone());
        let last = name.rsplit('/').next().unwrap();
        read_of.push(read::script_excerpt(last, holds));
        if read_of.len() == 2 {
            break;
        }
    }
    read_of
}

#[test]
fn an_app_is_answered_as_the_python_hosts_service_answers() {
    let vectors = vectors();
    let key = vectors["identity_key"].as_str().unwrap();
    let (mut scenes, mut requests, mut refused, mut batches, mut parts, mut learned) =
        (0, 0, 0, 0, 0, 0);
    let mut where_python_fails = 0;
    for scene in vectors["scenes"].as_array().unwrap() {
        let name = scene["name"].as_str().unwrap();
        let pace = Pace {
            debounce_ms: scene["debounce_ms"].as_i64().unwrap(),
            failure_ms: scene["failure_ttl_ms"].as_i64().unwrap(),
        };
        let mut desk = Desk::new(key, pace, Vec::<Row>::new(), Vec::new());
        let mut janitor = Janitor {
            model: scene["model"].as_str().unwrap().to_string(),
            identity: Some("as installed".to_string()),
            level: None,
            sources: 0,
        };
        let mut set = 0;
        let mut files: BTreeMap<String, String> = scene["files"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(name, holds)| (name.clone(), holds.as_str().unwrap().to_string()))
            .collect();
        let told = scene["asked"].as_array().unwrap();
        let mut now = 1_800_000_000_000_i64;
        let mut asked = 0;
        // Where the Python Host fails on a request (defect R39) and this desk answers, the
        // two are not in the same state afterwards, and are not compared any further.
        let mut apart = false;
        let mut settle =
            |desk: &mut Desk, janitor: &Janitor, now: &mut i64, asked: &mut usize, apart: bool| {
                for _ in 0..64 {
                    *now += pace.debounce_ms + 1;
                    while let Some(batch) = desk.claim(Some(janitor), *now) {
                        let how = match told.get(*asked) {
                            Some(theirs) => {
                                let theirs_about = about(theirs["requests"].as_array().unwrap());
                                assert_eq!(
                                    about(&batch.requests),
                                    theirs_about,
                                    "{name}: batch {asked}"
                                );
                                assert_eq!(batch.level, theirs["level"], "{name}: batch {asked}");
                                assert_eq!(batch.model, janitor.model, "{name}");
                                theirs["how"].as_str().unwrap()
                            }
                            None => {
                                assert!(
                                    apart,
                                    "{name}: the model is asked a time too many: {batch:?}"
                                );
                                "rule"
                            }
                        };
                        batches += 1;
                        parts += batch.requests.len();
                        *asked += 1;
                        *now += 1;
                        desk.complete(&model(how, batch.level, &batch.requests), *now);
                    }
                    if !desk.busy() {
                        return;
                    }
                }
                panic!("{name}: the desk does not come to rest");
            };
        for step in scene["steps"].as_array().unwrap() {
            now += 1;
            match step["do"].as_str().unwrap() {
                "ask" => {
                    requests += 1;
                    let with_scripts = step["cwd"] == true;
                    let read_scripts = |activity: &Map<String, Value>| {
                        if with_scripts {
                            scripts(&files, activity)
                        } else {
                            Vec::new()
                        }
                    };
                    let asking = Asked {
                        level: &step["level"],
                        items: &step["items"],
                        release: step.get("release"),
                        target: step.get("target"),
                        provenance: step["provenance"] == true,
                    };
                    let ours = desk.request(&asking, Some(&janitor), &read_scripts, now);
                    if let Some(words) = step.get("refused") {
                        assert_eq!(ours, Err(words.as_str().unwrap()), "{name}: {step}");
                        refused += 1;
                    } else if step.get("fails").is_some() {
                        assert!(ours.is_ok(), "{name}: {step}");
                        where_python_fails += 1;
                        apart = true;
                    } else if !apart {
                        let ours = ours.unwrap();
                        assert_eq!(ours, step["answer"], "{name}: {step}");
                        assert_eq!(
                            serde_json::to_string(&ours).unwrap(),
                            serde_json::to_string(&step["answer"]).unwrap(),
                            "{name}: {step}"
                        );
                    }
                }
                "settle" => {
                    settle(&mut desk, &janitor, &mut now, &mut asked, apart);
                    if !apart {
                        assert_eq!(asked, step["asked_so_far"], "{name}: {step}");
                    }
                }
                "wait" => now += step["ms"].as_i64().unwrap(),
                "janitor" => {
                    // What it is set to is another janitor to the desk, whichever way.
                    set += 1;
                    if let Some(options) = step.get("options") {
                        if let Some(level) = options["detail_level"].as_u64() {
                            janitor.level = Some(u8::try_from(level).unwrap());
                        }
                        if let Some(sources) = options["explanation_sources"].as_u64() {
                            janitor.sources = u8::try_from(sources).unwrap();
                        }
                        janitor.identity = Some(format!("set {set}"));
                    }
                    match step["enabled"].as_bool() {
                        Some(false) => janitor.identity = None,
                        Some(true) => janitor.identity = Some(format!("set {set}")),
                        None => {}
                    }
                }
                "write" => {
                    let file = step["file"].as_str().unwrap().to_string();
                    files.insert(file, step["text"].as_str().unwrap().to_string());
                }
                other => panic!("{name}: a step nobody knows: {other}"),
            }
        }
        settle(&mut desk, &janitor, &mut now, &mut asked, apart);
        scenes += 1;
        if apart {
            continue;
        }
        assert_eq!(asked, told.len(), "{name}: how often the model was asked");
        let ours: Vec<Value> = desk
            .table()
            .rows()
            .map(|row| {
                assert_eq!(row.prompt_version, PROMPT_VERSION);
                json!({
                    "signature": row.signature, "level": row.level,
                    "template_text": row.template_text, "slot_names": row.slot_names,
                    "program": row.program, "action": row.action, "producer": row.producer,
                    "parameterised": row.parameterised, "hits": row.hits,
                })
            })
            .collect();
        assert_eq!(
            Value::Array(ours.clone()),
            scene["learned"],
            "{name}: what was learned"
        );
        learned += ours.len();
        let said = desk.stats("24h", "hour", now).unwrap();
        assert_eq!(
            said["totals"]["counts"], scene["counts"],
            "{name}: what the ledger counted"
        );
        let mut totals = said["totals"].as_object().unwrap().clone();
        totals.shift_remove("counts");
        assert_eq!(
            Value::Object(totals),
            scene["totals"],
            "{name}: the ledger's sums"
        );
    }
    // Counted by the tool that wrote the vectors: 22 scenes, 104 requests of which 23 are
    // refused and 2 fail on the Python Host; the model was asked 32 times about 84 parts
    // there, and 76 rows were learned. Here the model is also asked about the commands the
    // Python Host fails on: one time and two parts more, in the last scene.
    assert_eq!(
        (scenes, requests, refused, where_python_fails),
        (22, 104, 23, 2)
    );
    assert_eq!((batches, parts, learned), (32 + 1, 84 + 2, 76));
}

#[test]
fn a_model_is_asked_in_the_python_hosts_words() {
    let vectors = vectors();
    let mut asked = 0;
    for case in vectors["prompted"].as_array().unwrap() {
        let level = u8::try_from(case[0].as_u64().unwrap()).unwrap();
        let ours = desk::prompt(level, case[1].as_array().unwrap());
        assert_eq!(ours, case[2].as_str().unwrap(), "{}", case[1]);
        asked += 1;
    }
    assert_eq!(asked, 12);
    // What a model answers with, as a table by number.
    let listed = json!({ "explanations": [
        { "id": "1", "text": "Lists files.", "template": "" },
        { "id": "2", "text": "Reads a.", "template": "Reads {path1}." },
    ] });
    let table = desk::tabled(&listed).unwrap();
    assert_eq!(
        table["2"],
        json!({ "text": "Reads a.", "template": "Reads {path1}." })
    );
    let twice = json!({ "explanations": [{ "id": "1", "text": "a" }, { "id": "1", "text": "b" }] });
    assert_eq!(desk::tabled(&twice), None);
    assert_eq!(desk::tabled(&json!(["a"])), None);
}

#[test]
fn the_ledger_says_what_the_python_hosts_says() {
    let vectors = vectors();
    let key = vectors["identity_key"].as_str().unwrap();
    let (mut asked, mut refused) = (0, 0);
    for ledger in vectors["ledgers"].as_array().unwrap() {
        let rows: Vec<Row> = ledger["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| Row {
                signature: row["signature"].as_str().unwrap().to_string(),
                level: u8::try_from(row["level"].as_u64().unwrap()).unwrap(),
                template_text: "A sentence.".to_string(),
                slot_names: Vec::new(),
                program: String::new(),
                action: String::new(),
                producer: "llm".to_string(),
                parameterised: row["parameterised"].as_bool().unwrap(),
                prompt_version: if row["current"] == true {
                    PROMPT_VERSION
                } else {
                    0
                },
                templates_version: clarp_kernel::explain::version(),
                created_at: 0,
                hits: row["hits"].as_u64().unwrap(),
                last_hit_at: None,
            })
            .collect();
        for case in ledger["asked"].as_array().unwrap() {
            let now = case["now"].as_i64().unwrap();
            // Nothing is counted before it happens: what the vectors count after the moment
            // that is asked about is not there yet.
            let mut desk = Desk::new(key, Pace::default(), rows.clone(), Vec::new());
            for counted in ledger["counted"].as_array().unwrap() {
                let at = counted[1].as_i64().unwrap();
                if at <= now {
                    desk.count(
                        counted[0].as_str().unwrap(),
                        at,
                        counted[2].as_i64().unwrap(),
                    );
                }
            }
            let window = case["window"].as_str().unwrap();
            let ours = desk.stats(window, case["bucket"].as_str().unwrap(), now);
            match case.get("refused") {
                Some(words) => {
                    assert_eq!(ours, Err(words.as_str().unwrap().to_string()), "{case}");
                    refused += 1;
                }
                None => {
                    let ours = ours.unwrap();
                    assert_eq!(ours, case["said"], "{} {window}", ledger["name"]);
                    assert_eq!(
                        serde_json::to_string(&ours).unwrap(),
                        serde_json::to_string(&case["said"]).unwrap(),
                        "{} {window}",
                        ledger["name"]
                    );
                }
            }
            asked += 1;
        }
    }
    assert_eq!((asked, refused), (16, 2));
}
