#![allow(clippy::unwrap_used)]
use clarp_kernel::call_transcription::{self as rules, Policy};
use serde_json::{Value, json};
fn texts(v: &Value) -> Vec<String> {
    serde_json::from_value(v.clone()).unwrap()
}

#[test]
fn a_calls_policy_matches_the_python_hosts_rules() {
    let v: Value =
        serde_json::from_str(include_str!("fixtures/call-transcription-vectors.json")).unwrap();
    const LOCAL: &str = "faster-whisper:small.en";
    let mut counts = [0; 4];
    for row in v["settings"].as_array().unwrap() {
        counts[0] += 1;
        let global = row["default"].as_str().unwrap();
        let p = Policy::read(
            row["old_language"].as_str().unwrap(),
            &texts(&row["old_engines"]).join(","),
            global,
            LOCAL,
        );
        match p.change(Some(&row["language"]), Some(&row["engines"]), global) {
            Ok(pair) => assert_eq!(json!(pair), row["ok"], "{row}"),
            Err(error) => assert_eq!(error, row["error"], "{row}"),
        }
    }
    for row in v["plans"].as_array().unwrap() {
        counts[1] += 1;
        let order = texts(&row["engines"]);
        assert_eq!(
            json!(rules::plan(&order, row["language"].as_str(), LOCAL)),
            row["steps"],
            "{row}"
        );
        assert_eq!(json!(rules::plan(&order, None, LOCAL)), row["plain"]);
    }
    for row in v["scopes"].as_array().unwrap() {
        counts[2] += 1;
        let id = row["id"].as_str().unwrap();
        let owner = row["owner"].as_str().unwrap();
        let call = &row["call"];
        let found = (!owner.is_empty() && call["call_id"] == id.trim()).then_some((owner, call));
        match rules::scope("me", "here", id, row["session"].as_str().unwrap(), found) {
            Ok(scope) => assert_eq!(json!(scope), row["ok"]),
            Err(e) => assert_eq!(json!([e.status, e.body]), row["error"]),
        }
    }
    for row in v["resolutions"].as_array().unwrap() {
        counts[3] += 1;
        let p = &row["policy"];
        let p = Policy::read(
            p["languages"].as_str().unwrap(),
            &texts(&p["engines"]).join(","),
            p["global_engine"].as_str().unwrap(),
            LOCAL,
        );
        assert_eq!(p.error, row["policy"]["error"]);
        let own = texts(&row["own"]);
        let resolve = || {
            p.resolve(
                row["ownlang"].as_str(),
                &own,
                "en",
                row["requested"].as_str().unwrap(),
            )
        };
        let resolved = resolve();
        let expected = &row["resolved"];
        assert_eq!(
            resolved.fields,
            json!({"language":expected["language"],"engines":expected["engines"]})
        );
        assert_eq!(json!(resolved.order), expected["_plan"]);
        assert_eq!(resolved.requested, expected["requested_model"]);
        let for_view = p.resolve(row["ownlang"].as_str(), &own, "en", "");
        assert_eq!(
            json!(rules::participants_view(
                &p,
                true,
                true,
                std::iter::once(("a".into(), for_view))
            )),
            row["view"]
        );
    }
    assert_eq!(counts, [540, 52, 720, 96]);
}

#[test]
fn each_attempt_records_only_what_its_engine_was_handed() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/call-transcription-vectors.json")).unwrap();
    let rows = fixture["attempts"].as_array().unwrap();
    for row in rows {
        let report = &row["report"];
        let language = row["language"].as_str().unwrap();
        let policy = Policy::read("", "", "local", "faster-whisper:small.en");
        let order = vec!["elevenlabs:scribe_v2".into(), "deepgram:nova-3".into()];
        let resolved = policy.resolve(Some(language), &order, "en", "");
        let mut seen = rules::Attempts::new(&resolved);
        let configured = row["configured"] == true;
        for step in rules::plan(&order, configured.then_some(language), &policy.local) {
            if let Some(reason) = step.skip {
                seen.skipped(&step.engine, reason);
                continue;
            }
            let handed = texts(&row["noted"])
                .contains(&step.engine)
                .then_some(step.language.flatten().unwrap_or_else(|| "auto".into()));
            if texts(&row["fails"]).contains(&step.engine) {
                seen.failed(
                    &step.engine,
                    handed.as_deref(),
                    125,
                    &format!("failed {}{}", step.engine, "x".repeat(210)),
                );
            } else {
                seen.heard(
                    &step.engine,
                    handed.as_deref(),
                    125,
                    step.adapted == Some(true),
                );
                break;
            }
        }
        assert_eq!(seen.finish(), *report, "{row}");
    }
    assert_eq!(rows.len(), 24);
}

#[test]
fn participant_views_are_derived_only_when_the_call_exposes_them() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/call-transcription-vectors.json")).unwrap();
    let rows = fixture["views"].as_array().unwrap();
    for row in rows {
        let policy = &row["policy"];
        let mut policy = Policy::read(
            policy["languages"].as_str().unwrap(),
            &texts(&policy["engines"]).join(","),
            "local",
            "faster-whisper:small.en",
        );
        policy.error = row["policy"]["error"].as_str().unwrap().into();
        let participants = ["a", "b"].into_iter().map(|session| {
            let own = (session == "a").then(|| row["language"].as_str().unwrap());
            (session.into(), policy.resolve(own, &[], "en", ""))
        });
        let view = rules::participants_view(
            &policy,
            row["state"] == "live",
            row["mode"] == "listen_first",
            participants,
        );
        assert_eq!(json!(view), row["view"], "{row}");
    }
    assert_eq!(rows.len(), 96);
}

#[test]
fn job_identity_binds_the_validated_call_fields_as_utf8_netstrings() {
    use clarp_kernel::heard::scoped_fingerprint;
    let call_id = "gcl_0123456789abcdef0123456789abcdef";
    let print = |scope: Option<&rules::Scope>| {
        scoped_fingerprint(b"clip", "audio/webm", "deepgram:nova-3", true, scope)
    };
    assert_eq!(
        print(None),
        "a53429870cbca3ab972a9509f178edf021a105a41a190330fb191dd09eaaadcb"
    );
    for (session, expected) in [
        (
            "0",
            "7f21c49004a7040f6d27af0154376580a5abba9ad804f9d4c4e647ef30213368",
        ),
        (
            "1",
            "55865e32a3c4f90de870fdba969a01a6c7c3284b020b91778d05ea007cc0d5db",
        ),
        (
            "comma,colon:",
            "b5e386a59f6cc5427225b772c0e8e56ba4b6cdbc2e10d7dccac0c192971e30be",
        ),
        (
            "h\u{f8}r",
            "84df2d39786d3fc47543c05d85804b0d613aca9303974419f214e6f93846cb05",
        ),
        (
            "mike",
            "562c901b36d4f7c4f563ae41627cfe28f2f1fa40982067d65648510286506da1",
        ),
    ] {
        let call = json!({"call_id":call_id,"host_id":"host","state":"live","participants":[{"session":session,"state":"active"}]});
        let valid = rules::scope(
            "administrator",
            "host",
            &format!(" {call_id} "),
            &format!(" {session} "),
            Some(("administrator", &call)),
        )
        .unwrap();
        assert_eq!(print(Some(&valid)), expected, "{session}");
    }
}

// Release-owner literals for R87, 19 November 2026.
#[test]
fn fingerprint_bytes_and_sha256_match_the_three_r87_vectors() {
    use clarp_kernel::heard::{fingerprint_input, scoped_fingerprint};
    const AUDIO: &[u8] = b"\x00\x01clarp-r87-audio\xff";
    const TYPE: &str = "audio/wav";
    const MODEL: &str = "whisper-1";
    const HANDS_FREE: bool = true;
    const PRINCIPAL: &str = "administrator";
    const CALL: &str = "gcl_0123456789abcdef0123456789abcdef";
    const VECTORS: [(Option<&str>, &str, &str); 3] = [
        (
            None,
            "0001636c6172702d7238372d617564696fff00617564696f2f77617600776869737065722d310031",
            "13aca67f9579d11a45af334f650855d9b18c1c6d4f68fe73ca16f460469b9c30",
        ),
        (
            Some("mike"),
            "0001636c6172702d7238372d617564696fff00617564696f2f77617600776869737065722d3100310063616c6c0031333a61646d696e6973747261746f722c33363a67636c5f30313233343536373839616263646566303132333435363738396162636465662c343a6d696b652c",
            "2f1d94bc33119437b1743bbc4b8c50e71cbc9ea3436c0adfa473a46efa37606b",
        ),
        (
            Some("0\0,:\u{e9}"),
            "0001636c6172702d7238372d617564696fff00617564696f2f77617600776869737065722d3100310063616c6c0031333a61646d696e6973747261746f722c33363a67636c5f30313233343536373839616263646566303132333435363738396162636465662c363a30002c3ac3a92c",
            "b50a97539d84f8306a0a00fac2ea07ca640ffcf741d28a6fb83d636a8ddf342d",
        ),
    ];
    for (session, hex, sha) in VECTORS {
        let scope = session.map(|session| rules::Scope {
            principal: PRINCIPAL.into(),
            call_id: CALL.into(),
            session: session.into(),
        });
        let mut input = Vec::new();
        fingerprint_input(AUDIO, TYPE, MODEL, HANDS_FREE, scope.as_ref(), |bytes| {
            input.extend_from_slice(bytes)
        });
        let actual: String = input.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(actual, hex);
        assert_eq!(
            scoped_fingerprint(AUDIO, TYPE, MODEL, HANDS_FREE, scope.as_ref()),
            sha
        );
    }
}
