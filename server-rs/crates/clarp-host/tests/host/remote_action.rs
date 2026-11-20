use super::*;

#[test]
fn shortcuts_broadcast_once_to_live_listeners_and_never_replay_a_button_press() {
    let place=Place::new("remote-action");
    let host=place.start_with_token("secret");
    let info=host.get("/server-info");
    assert_eq!(info["contract"]["features"]["remote_action"],1);
    assert!(info["capabilities"]["features"].as_array().unwrap().contains(&json!("remote_action")));
    let (limited,_)=pair(&host,"Button","limited");
    let listen=||host.listen_at(&format!("/events?token={limited}&last_event_id=0"));
    let heard=listen();
    let ready=|events:&Arc<Mutex<Vec<Value>>>|wait_for("listener's roster",||events.lock().unwrap().iter().any(|e|e==&json!({"type":"agent-roster"})));
    ready(&heard);
    let get=|action:&str|fetch(&host,&limited,"GET",&format!("/remote-action?action={action}"),&[],&[]);
    for action in ["record","record-toggle","stop-agent"] {
        let page=get(action);
        assert_eq!(page.status,200);
        assert_eq!(page.header("content-type"),"text/html");
        assert_eq!(page.header("cache-control"),"no-store");
        assert_eq!(page.text(),"<!doctype html><meta charset=utf-8><title>ok</title>");
        assert_eq!(host.ask_as(&limited,"POST","/remote-action",Some(&json!({"action":action}))), (200,json!({"ok":true})));
    }
    for action in ["controller-event","other"] {
        let refused=get(action);
        assert_eq!(refused.status,400);
        assert_eq!(serde_json::from_slice::<Value>(&refused.body).unwrap(),json!({"error":"unknown action"}));
    }
    let post=|body:Value|host.ask_as(&limited,"POST","/remote-action",Some(&body));
    assert_eq!(post(json!({"action":"controller-event"})),(400,json!({"error":"button required"})));
    assert_eq!(post(json!({"action":"other"})),(400,json!({"error":"unknown action"})));
    assert_eq!(host.ask_as("","POST","/remote-action",Some(&json!({"action":"record"}))).0,401);
    let press=json!({"action":"controller-event","button":"primary","controller_event":"down","ts":123,"controller_id":"shortcut"});
    let (status,reply)=post(press.clone());
    assert_eq!(status,200);
    let id=reply["controller_event_id"].as_str().unwrap();
    assert_eq!(id.len(),16);
    assert!(id.bytes().all(|b|b.is_ascii_alphanumeric() || matches!(b,b'_'|b'-')));
    let mut release=press;
    release["controller_event"]=json!("up");
    release["controller_event_id"]=json!("release");
    assert_eq!(post(release),(200,json!({"ok":true,"controller_event_id":"release"})));
    let actions=|events:&Arc<Mutex<Vec<Value>>>|->Vec<Value>{events.lock().unwrap().iter().filter(|e|e["type"]=="remote-action").cloned().collect()};
    wait_for("release after the earlier presses",||actions(&heard).iter().any(|e|e["controller_event_id"]=="release"));
    let seen=actions(&heard);
    assert_eq!(seen.iter().map(|e|e["action"].as_str().unwrap()).collect::<Vec<_>>(),["record","record","record-toggle","record-toggle","stop-agent","stop-agent","controller-event","controller-event"]);
    for e in &seen[..6]{assert!(e["ts"].as_i64().unwrap()>0); assert_eq!(e.as_object().unwrap().len(),3);}
    assert_eq!(seen[6],json!({"type":"remote-action","action":"controller-event","button":"primary","controller_event":"down","duration_ms":0,"age_ms":0,"queued":false,"controller_id":"shortcut","ts":123,"controller_event_id":id}));
    let later=listen();
    ready(&later);
    assert_eq!(get("record").status,200);
    wait_for("new listener hears the next press",||!actions(&later).is_empty());
    assert_eq!(actions(&later).iter().map(|e|e["action"].clone()).collect::<Vec<_>>(),[json!("record")]);
}
