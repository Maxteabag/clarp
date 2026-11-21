use clarp_kernel::takeover::{self, ClaudeFacts};
use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
struct Vectors {
    uuids: Vec<UuidCase>,
    launches: Vec<LaunchCase>,
    streams: Vec<StreamCase>,
    transcripts: Vec<TranscriptCase>,
    agy_guards: Vec<AgyCase>,
}

#[derive(Deserialize)]
struct UuidCase {
    trace: String,
    uuid: String,
}

#[derive(Deserialize)]
struct LaunchCase {
    trace: String,
    first: Option<String>,
    second: String,
    line: Value,
}

#[derive(Deserialize)]
struct StreamCase {
    provider: String,
    event: Value,
    output: bool,
}

#[derive(Deserialize)]
struct TranscriptCase {
    transcript: Option<String>,
    prompt: String,
    facts: ClaudeFacts,
}

#[derive(Deserialize)]
struct AgyCase {
    turns: Option<Vec<Value>>,
    first: String,
    allowed: bool,
}

#[test]
fn provider_takeover_evidence_matches_the_reference() {
    let vectors: Vectors =
        serde_json::from_str(include_str!("fixtures/takeover-vectors.json")).unwrap();
    let mut count = 0;
    for case in vectors.uuids {
        assert_eq!(
            takeover::prompt_uuid(&case.trace).as_deref(),
            Some(case.uuid.as_str()),
            "{:?}",
            case.trace
        );
        count += 1;
    }
    assert_eq!(takeover::prompt_uuid("\t \n"), None);
    for case in vectors.launches {
        let uuid = takeover::prompt_uuid(&case.trace).unwrap();
        let texts: Vec<_> = case
            .first
            .as_deref()
            .into_iter()
            .chain([case.second.as_str()])
            .collect();
        assert_eq!(takeover::claude_user_line(&uuid, &texts), case.line);
        count += 1;
    }
    for case in vectors.streams {
        let output = match case.provider.as_str() {
            "claude" => takeover::claude_output(&case.event),
            "agy" => takeover::agy_output(&case.event),
            id => panic!("unexpected provider {id}"),
        };
        assert_eq!(output, case.output, "{}: {}", case.provider, case.event);
        count += 1;
    }
    for case in vectors.transcripts {
        assert_eq!(
            takeover::claude_facts(case.transcript.as_deref(), &case.prompt),
            case.facts,
            "{:?}",
            case.transcript
        );
        count += 1;
    }
    for case in vectors.agy_guards {
        assert_eq!(
            takeover::agy_can_take_over(case.turns.as_deref(), &case.first),
            case.allowed,
            "{:?}",
            case.turns
        );
        count += 1;
    }
    assert_eq!(count, 49);
    println!("{count} provider takeover reference cases");
}
