use super::*;

#[test]
fn new_agent_directory_suggestions_and_frequent_paths_survive_restart() {
    let place = Place::new("launch-directories");
    let home = place.root.join("synthetic-home");
    let one = home.join("Project one");
    let two = home.join("Project two");
    let fallback_only = home.join("Project twelve");
    for p in [&one, &two, &fallback_only] {
        std::fs::create_dir_all(p).unwrap();
    }
    let program_path = place.root.join("zoxide-stand-in");
    program(
        &program_path,
        r#"#!/usr/bin/env python3
import json,os,pathlib,sys,time
home=pathlib.Path(os.environ["HOME"])
with (home/"requests").open("a") as log:
    print(json.dumps({"args":sys.argv[1:],"secret":"CLARP_HOST_TOKEN" in os.environ,
                      "identity":"CLARP_SESSION" in os.environ}),file=log)
if "zzzzzz" in sys.argv:
    (home/"child-pid").write_text(str(os.getpid()))
    time.sleep(2)
    (home/"late").touch()
if "--" not in sys.argv:
    print(home/"Project twelve")
    sys.exit(3)
if "ptw" in sys.argv:sys.exit(1)
print(home/"Project one")
"#,
    );
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_ZOXIDE_PROGRAM", program_path.to_str().unwrap()),
        ("CLARP_SESSION", "synthetic-parent"),
    ];
    let start = || place.start_in("fixture", &[], &world);
    let host = start();
    let (limited, _) = pair(&host, "list reader", "limited");
    let (full, _) = pair(&host, "full reader", "full");
    let lookup = "/launch-directories";
    let fav = "/favorite-paths";
    assert_eq!(host.ask_as("wrong", "GET", lookup, None).0, 401);
    assert_eq!(host.ask_as("wrong", "GET", fav, None).0, 401);
    let refused = host.ask_as(&limited, "GET", "/dirs", None);
    assert_eq!(refused.0, 403);
    assert_eq!(host.ask_as(&limited, "GET", lookup, None), refused);
    assert_eq!(host.ask_as(&limited, "GET", fav, None), refused);
    assert_eq!(host.ask_as(&limited, "POST", fav, Some(&json!({}))).0, 403);
    let refusal = fetch(&host, &full, "POST", fav, &[], b"{}");
    assert_eq!(
        (refusal.status, refusal.text()),
        (404, "not found".to_owned())
    );
    assert_eq!(host.get(fav), json!({"paths":[]}));
    assert!(!home.join("requests").exists());
    let make = |name: &str, cwd: &PathBuf| {
        host.post(
            "/agents",
            json!({"name":name,"cwd":cwd,"backend":"scripted"}),
        )
    };
    let made = make("First", &one);
    let first = host.get(fav)["paths"][0].clone();
    assert_eq!(first["path"], json!(one));
    assert_eq!(first["use_count"], 1);
    assert_eq!(first["first_used_at"], first["last_used_at"]);
    make("Second", &two);
    host.post(
        "/agents",
        json!({"replace_sid":made["session"],"name":"First","cwd":two}),
    );
    let favorite = host.get(fav);
    assert_eq!(favorite["paths"][0]["path"], json!(two));
    assert_eq!(favorite["paths"][0]["use_count"], 2);
    assert_eq!(favorite["paths"][1], first);
    assert_eq!(
        host.ask_as(&full, "GET", fav, None),
        (200, favorite.clone())
    );
    assert_eq!(
        host.get("/favorite-paths?limit=1")["paths"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        host.ask_as(
            &full,
            "GET",
            "/launch-directories?q=&q=Project+two&q=ignored",
            None
        )
        .1,
        json!({"home":home,"query":"Project two","matches":[{"path":one,"label":"~/Project one"},{"path":two,"label":"~/Project two"}]})
    );
    assert_eq!(
        host.get("/launch-directories?q=.%2FProject+two")["matches"][0]["path"],
        json!(two)
    );
    assert_eq!(
        host.get("/launch-directories?q=ptw")["matches"],
        json!([{"path":fallback_only,"label":"~/Project twelve"},{"path":two,"label":"~/Project two"}])
    );
    let empty = host.get(lookup);
    assert_eq!(
        empty["matches"],
        json!([{"path":home,"label":"~"},{"path":two,"label":"~/Project two"},{"path":one,"label":"~/Project one"}])
    );
    let before = Instant::now();
    assert_eq!(
        host.get("/launch-directories?q=zzzzzz")["matches"],
        json!([])
    );
    assert!(before.elapsed() < Duration::from_secs(2));
    let pid = std::fs::read_to_string(home.join("child-pid")).unwrap();
    wait_for("timed-out zoxide to exit", || {
        !PathBuf::from("/proc").join(pid.trim()).exists()
    });
    assert!(!home.join("late").exists());
    let rows: Vec<Value> = std::fs::read_to_string(home.join("requests"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert!(
        rows.iter()
            .all(|r| r["secret"] == false && r["identity"] == false)
    );
    assert_eq!(
        rows.iter().map(|r| r["args"].clone()).collect::<Vec<_>>(),
        vec![
            json!(["query", "--list", "--", "Project", "two"]),
            json!(["query", "--list", "--", "./Project", "two"]),
            json!(["query", "--list", "--", "ptw"]),
            json!(["query", "--list"]),
            json!(["query", "--list", "--"]),
            json!(["query", "--list", "--", "zzzzzz"])
        ]
    );
    assert_eq!(host.get(fav), favorite);
    drop(host);
    let kept = kept_records(&place, "launch-path-usage");
    assert_eq!(kept.len(), 2);
    assert_eq!(kept[two.to_str().unwrap()], favorite["paths"][0]);
    std::fs::remove_file(&program_path).unwrap();
    let host = start();
    assert_eq!(host.get(fav), favorite);
    assert_eq!(host.get(lookup), empty);
    std::fs::remove_dir(&two).unwrap();
    assert_eq!(
        host.get("/launch-directories?q=Project+two")["matches"],
        json!([])
    );
    assert_eq!(host.get(fav), favorite);
    let private = home.join("private-work");
    std::fs::create_dir(&private).unwrap();
    let janitor = json!({"name":"Workspace Check","backend":"codex","cwd":private,
        "request_id":"test-janitor","template_id":"scheduled-prompt","options":{"instructions":"Check."},
        "attachments":[{"attachment_id":"check","trigger_id":"schedule","config":{"cron":"15 3 * * *","timezone":"UTC"}}]});
    let once = host.post("/janitors", janitor.clone());
    let replay = host.post("/janitors", janitor);
    assert_eq!(once["janitor"]["agent_id"], replay["janitor"]["agent_id"]);
    let prefs = host.get(fav);
    let count = |v: &Value| {
        v["paths"]
            .as_array()
            .unwrap()
            .iter()
            .find(|u| u["path"] == json!(private))
            .unwrap()["use_count"]
            .clone()
    };
    assert_eq!(count(&prefs), 1);
    assert!(
        !host.get(lookup)["matches"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["path"] == json!(private))
    );
    host.post(
        "/agents",
        json!({"name":"Shared Workspace","backend":"scripted","cwd":private}),
    );
    assert_eq!(count(&host.get(fav)), 2);
    assert!(
        host.get(lookup)["matches"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["path"] == json!(private))
    );
    let before = host.get(fav);
    let refused = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name":"First","backend":"scripted","cwd":private})),
    );
    assert!(refused.0 >= 400);
    assert_eq!(host.get(fav), before);
}

#[test]
fn unreadable_launch_usage_is_skipped_until_that_directory_is_launched_again() {
    use clarp_ports::kv::KeyValueEngine as _;
    let place = Place::new("unreadable-launch-usage");
    let start = || place.start_with_token("fixture");
    let good = place.root.join("good");
    let broken = place.root.join("private-directory-name");
    for dir in [&good, &broken] {
        std::fs::create_dir(dir).unwrap();
    }
    let host = start();
    host.post(
        "/agents",
        json!({"name":"Good","backend":"scripted","cwd":good}),
    );
    let original = host.get("/favorite-paths");
    assert_eq!(original["paths"][0]["path"], json!(good));
    drop(host);
    let open = || {
        clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap()
    };
    let shelf = clarp_host::fleet::shelf("launch-path-usage");
    let key = broken.to_str().unwrap();
    let corrupt = b"unreadable-private-value";
    {
        let mut store = open();
        store.put(&shelf, key, corrupt).unwrap();
        store.put(&shelf, "another-private-key", b"{}").unwrap();
    }
    let host = start();
    assert_eq!(host.get("/favorite-paths"), original);
    let journal = host.heard.lock().unwrap().clone();
    assert_eq!(
        journal
            .lines()
            .filter(|l| l.contains("launch-path-usage"))
            .collect::<Vec<_>>(),
        [
            "clarp-host: a record of the kind launch-path-usage could not be read and is set aside beside it, 1 of this kind so far: its desk starts without it",
            "clarp-host: a record of the kind launch-path-usage could not be read and is set aside beside it, 2 of this kind so far: its desk starts without it",
            "clarp-host: 2 records read at the start could not be read and are left out (launch-path-usage 2)",
        ]
    );
    for private in [key, "another-private-key", "unreadable-private-value"] {
        assert!(!journal.contains(private));
    }
    drop(host);
    // Moved aside whole, never lost: the key is free, its copy stands beside it.
    assert_eq!(open().get(&shelf, key).unwrap(), None);
    assert_eq!(
        open()
            .get(&shelf, &format!("{key}.unreadable"))
            .unwrap()
            .as_deref(),
        Some(corrupt.as_slice())
    );
    let host = start();
    host.post(
        "/agents",
        json!({"name":"Repaired","backend":"scripted","cwd":broken}),
    );
    let after = host.get("/favorite-paths");
    let repaired = after["paths"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["path"] == key)
        .unwrap();
    assert_eq!(repaired["use_count"], 1);
    assert_eq!(repaired["first_used_at"], repaired["last_used_at"]);
    drop(host);
    let host = start();
    assert_eq!(host.get("/favorite-paths"), after);
    // The copies stand, and are counted at every start until somebody removes them.
    assert_eq!(host.get("/host/start")["left_out"]["launch-path-usage"], 2);
}
