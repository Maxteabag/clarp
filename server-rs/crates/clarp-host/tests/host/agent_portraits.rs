//! The pictures a client can choose to display for a model and a maintenance role.
use super::*;

#[test]
fn the_picture_display_preference_survives_restart_and_uses_local_art() {
    let place = Place::new("portrait-preference");
    let root = place.root.join("static");
    let config = place.root.join("owner.toml");
    std::fs::write(&config, "[agents]\nclaude_model='claude-sonnet-test'\n").unwrap();
    std::fs::create_dir_all(root.join("avatars/models")).unwrap();
    std::fs::create_dir_all(root.join("janitor-avatars")).unwrap();
    std::fs::write(root.join("avatars/models/sample.sonnet.png"), b"model art").unwrap();
    std::fs::write(root.join("janitor-avatars/delegator-message-dispatcher.jpg"), b"role art").unwrap();
    std::fs::write(root.join("janitor-avatars/janitor-default.jpg"), b"fallback").unwrap();
    let args = ["--static", root.to_str().unwrap(), "--speech-keys", config.to_str().unwrap()];
    let host = place.start_without("fixture", &args, &[], &["OPENAI_API_KEY"]);
    let settings = "/avatar-settings";
    // The preference is off initially. Readers still get the URL of any bundled model art.
    assert_eq!(host.get(settings), json!({"model_avatars":false}));
    let made = host.post("/agents", json!({"name":"Sample","backend":"claude"}));
    let session = made["session"].as_str().unwrap();
    let row = |snapshot:&Value, session:&str| -> Value {
        snapshot["agents"].as_array().unwrap().iter().find(|a| a["session"]==session).unwrap().clone()
    };
    let snapshot = host.get("/agents/snapshot");
    assert_eq!(snapshot["model_avatars"],false);
    let drawn = row(&snapshot,session)["model_avatar_url"].as_str().unwrap().to_string();
    assert!(drawn.starts_with("/static/avatars/models/sample.sonnet.png?v="),"{drawn}");
    assert_eq!(fetch(&host,"","GET",&drawn,&[],b"").body,b"model art");
    // A janitor wears the bundled picture for its role, with the generic picture as fallback.
    for (sid, file, bytes) in [
        ("clarp-message-delegator","delegator-message-dispatcher.jpg","role art"),
        ("clarp-goal-keeper","janitor-default.jpg","fallback"),
    ] {
        let janitor = row(&snapshot,sid);
        let url = janitor["avatar_url"].as_str().unwrap();
        assert!(url.starts_with(&format!("/static/janitor-avatars/{file}?v=")), "{url}");
        assert_eq!(fetch(&host,"","GET",url,&[],b"").body,bytes.as_bytes());
    }
    // The settings belong to a full-access caller. Each refused update preserves the old value.
    let (limited,_) = pair(&host,"Limited","limited");
    for method in ["GET","POST"] {
        assert_eq!(host.ask_as(&limited,method,settings,Some(&json!({"model_avatars":true}))).0,403);
    }
    for body in [json!({}),json!({"model_avatars":1}),json!({"model_avatars":"true"})] {
        assert_eq!(host.ask("POST",settings,Some(&body)),(400,json!({"error":"model_avatars required"})));
    }
    assert_eq!(host.post(settings,json!({"model_avatars":true})),json!({"model_avatars":true}));
    assert_eq!(host.get("/agents/snapshot")["model_avatars"],true);
    host.kill();
    let host = place.start_without("fixture", &args, &[], &["OPENAI_API_KEY"]);
    assert_eq!(host.get(settings),json!({"model_avatars":true}));
    assert_eq!(host.post(settings,json!({"model_avatars":false})),json!({"model_avatars":false}));
    assert_eq!(host.get("/agents/snapshot")["model_avatars"],false);
}
