use super::*;

#[test]
fn agent_creation_followup_uses_cli_model_defaults_and_explicit_session_spelling() {
    let place = Place::new("creation-cli-defaults");
    let codex = place.root.join("fixture-codex");
    let claude = place.root.join("fixture-claude");
    std::fs::create_dir(&codex).unwrap();
    std::fs::create_dir(&claude).unwrap();
    std::fs::write(
        codex.join("config.toml"),
        "model = 'top-model'\nprofile = 'picked'\n[profiles.picked]\nmodel = 'profile-model'\n",
    )
    .unwrap();
    std::fs::write(
        claude.join("settings.json"),
        br#"{"model":"claude-settings"}"#,
    )
    .unwrap();
    let host = place.start_in(
        "",
        &[],
        &[
            ("CODEX_HOME", codex.to_str().unwrap()),
            ("CLAUDE_CONFIG_DIR", claude.to_str().unwrap()),
            ("ANTHROPIC_MODEL", "claude-environment"),
        ],
    );
    let codex = host.post(
        "/agents",
        json!({
            "name":"Codex fixture", "backend":"codex", "session":"ré sumé",
        }),
    );
    assert_eq!(codex["session"], "résumé");
    assert_eq!(codex["agent"]["model"], "profile-model");
    let claude = host.post(
        "/agents",
        json!({"name":"Claude fixture","backend":"claude"}),
    );
    assert_eq!(claude["agent"]["model"], "claude-environment");
    let reset = host.post(
        "/agents",
        json!({
            "name":"Codex fixture", "replace_sid":"résumé", "model":null,
        }),
    );
    assert_eq!(reset["agent"]["model"], "profile-model");
}

#[test]
fn agent_creation_followup_relaunch_uses_persona_presentation_and_refuses_custom_voice_collisions()
{
    let place = Place::new("creation-presentation");
    let host = place.start();
    let (status, persona) = host.ask(
        "POST",
        "/personas",
        Some(&json!({
            "name":"Presentation fixture", "avatar_symbol":"persona-symbol", "generate_avatar":false,
        })),
    );
    assert_eq!(status, 201, "{persona}");
    let voice = r#"{"cartesia":"fixture-cartesia"}"#;
    let made = host.post(
        "/agents",
        json!({
            "name":"Presentation fixture", "backend":"scripted", "avatar_symbol":"own-symbol",
            "avatar_base64":clarp_kernel::python::b64encode(b"synthetic picture"), "voice_id":voice,
        }),
    );
    assert_eq!(made["agent"]["avatar_symbol"], "own-symbol");
    assert!(!made["agent"]["avatar_url"].as_str().unwrap().is_empty());
    let (status, error) = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name":"Different fixture", "backend":"scripted", "voice_id":voice})),
    );
    assert_eq!(status, 409);
    assert_eq!(
        error,
        json!({"error":"voice_in_use", "message":"That voice is already used by Presentation fixture."})
    );
    let changed = host.post(
        "/agents",
        json!({
            "name":"Presentation fixture", "replace_sid":made["session"],
            "avatar_symbol":null, "avatar_path":"/synthetic/ignored",
        }),
    );
    assert_eq!(changed["agent"]["avatar_symbol"], "persona-symbol");
    assert_eq!(changed["agent"]["avatar_url"], "");
    assert_eq!(changed["agent"]["voice_id"], made["agent"]["voice_id"]);
}

#[test]
fn agent_creation_details_returns_the_named_launch_fields() {
    let place = Place::new("named-agent-creation");
    let host = place.start();
    let info = host.get("/server-info");
    assert_eq!(info["contract"]["features"]["agent_creation_details"], 1);
    assert!(
        info["capabilities"]["features"]
            .as_array()
            .unwrap()
            .contains(&json!("agent_creation_details"))
    );
    let cwd = place.root.join("work");
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({
            "name":"  Named fixture  ", "backend":" Codex ", "cwd":cwd,
            "model":" fixture-model ", "effort":" HIGH ", "session":"fixture-with_details",
            "avatar_symbol":"fixture-symbol", "synthesize_audio":false, "voice_id":"fixture-voice",
        })),
    );
    assert_eq!(status, 200, "{made}");
    assert_eq!(made["ok"], true);
    assert_eq!(made["session"], "fixture-with_details");
    assert_eq!(made["name"], "Named fixture");
    let row = &made["agent"];
    let expected = json!({
        "session":"fixture-with_details", "persona":"Named fixture", "backend":"codex",
        "cwd":cwd, "model":"fixture-model", "effort":"high", "avatar_symbol":"fixture-symbol",
        "muted":0, "heartbeat_enabled":0, "dreaming_enabled":0, "parent_agent_id":null,
        "role":"agent", "helper_state":null, "alive":true, "latest_state":"spawned",
        "avatar_url":"", "avatar_generation_status":null,
    });
    for (key, value) in expected.as_object().unwrap() {
        assert_eq!(&row[key], value, "{key}");
    }
    assert!(row["agent_id"].as_str().is_some_and(|id| !id.is_empty()));
    assert!(
        row["last_activity"]
            .as_i64()
            .is_some_and(|ms| ms > 1_000_000_000_000)
    );
    assert!(
        row["voice_id"]
            .as_str()
            .is_some_and(|voice| voice.contains("fixture-voice"))
    );
    assert!(row["voice_profile"].is_object());
    // An omitted pin stays, an explicitly null pin resets it to the backend default.
    let session = made["session"].as_str().unwrap();
    let retained = host.post(
        "/agents",
        json!({"name":"Named fixture","replace_sid":session}),
    );
    assert_eq!(retained["agent"]["model"], "fixture-model");
    assert_eq!(retained["agent"]["effort"], "high");
    let cleared = host.post(
        "/agents",
        json!({"name":"Named fixture","replace_sid":session,"model":null,"effort":null}),
    );
    assert_eq!(cleared["agent"]["model"], "");
    assert_eq!(cleared["agent"]["effort"], "");
}

#[test]
fn agent_creation_details_anonymous_launch_chooses_its_own_identity() {
    let place = Place::new("anonymous-agent-creation");
    let host = place.start();
    let request = json!({
        "anonymous": true, "backend": "codex", "name": "Ignored", "session": "ignored",
        "voice_id": "ignored", "avatar_symbol": "ignored", "personality": "ignored",
    });
    let first = host.post("/agents", request.clone());
    let second = host.post("/agents", request);
    for made in [&first, &second] {
        let name = made["name"].as_str().unwrap();
        let suffix = name.strip_prefix("Codex-").unwrap();
        assert_eq!(suffix.len(), 4);
        assert!(suffix.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_eq!(made["agent"]["persona"], made["name"]);
        assert_eq!(made["agent"]["backend"], "codex");
        assert_eq!(made["agent"]["avatar_symbol"], "");
        assert_ne!(made["agent"]["voice_id"], "ignored");
        assert_ne!(made["session"], "ignored");
    }
    assert_ne!(first["name"], second["name"]);
    assert_ne!(first["session"], second["session"]);

    for (key, value, expected) in [
        (
            "replace_sid",
            first["session"].clone(),
            "anonymous launch must be fresh",
        ),
        ("mint_contact", json!(true), "conflicting launch options"),
        ("auto_contact", json!(true), "conflicting launch options"),
    ] {
        assert_eq!(
            host.ask(
                "POST",
                "/agents",
                Some(&json!({"anonymous":true, key:value}))
            ),
            (400, json!({"error":expected,"message":expected}))
        );
    }
}

#[test]
fn agent_creation_details_relaunch_updates_picture_and_lineage() {
    let place = Place::new("relaunch-agent-creation");
    let host = place.start();
    let parent = host.post("/agents", json!({"name":"Parent","backend":"scripted"}));
    let original = host.post("/agents", json!({"name":"Relaunched","backend":"scripted"}));
    let session = original["session"].as_str().unwrap();
    let image = b"synthetic avatar bytes";
    let request = json!({
        "name": "Relaunched", "replace_sid": session,
        "avatar_base64": clarp_kernel::python::b64encode(image),
        "parent": parent["session"], "role": "helper",
    });
    let made = host.post("/agents", request);
    let row = &made["agent"];
    assert_eq!(row["agent_id"], original["agent"]["agent_id"]);
    assert_eq!(row["session"], session);
    assert_eq!(row["parent_agent_id"], parent["agent"]["agent_id"]);
    assert_eq!(row["role"], "helper");
    assert_eq!(row["helper_state"], "running");
    let url = row["avatar_url"].as_str().unwrap();
    assert_eq!(fetch(&host, &host.token, "GET", url, &[], b"").body, image);

    let again = host.post(
        "/agents",
        json!({"name":"Relaunched","replace_sid":session}),
    );
    for key in ["parent_agent_id", "role", "helper_state", "avatar_url"] {
        assert_eq!(again["agent"][key], row[key], "{key}");
    }
    let refused = |replace: &Value, name: &str, parent: &str, why: &str| {
        let (status, body) = host.ask(
            "POST",
            "/agents",
            Some(&json!({"name": name, "replace_sid":replace, "parent":parent})),
        );
        assert_eq!(status, 409);
        assert_eq!(body["error"], why);
    };
    refused(&made["session"], "Relaunched", session, "self_parent");
    refused(&parent["session"], "Parent", session, "parent_cycle");
    let (status, invalid) = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name":"Relaunched","replace_sid":session,"avatar_base64":"%%%"})),
    );
    assert_eq!(status, 400);
    assert_eq!(invalid["error"], "invalid avatar");
    assert_eq!(fetch(&host, &host.token, "GET", url, &[], b"").body, image);
}
