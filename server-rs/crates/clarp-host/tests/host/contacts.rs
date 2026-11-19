//! The Host's tests of who an agent is when nobody named it: the pool, the mint, `POST /agent-assign`: one file a piece, declared in `host.rs`.

use super::*;
#[test]
fn an_agent_nobody_named_takes_a_free_contact_or_one_minted_for_it_and_a_running_one_is_assigned_one()
 {
    let place = Place::new("contacts");
    let host = place.start();
    let events = host.listen();
    let personas = |host: &Host| {
        host.get("/agents/snapshot")["personas"]
            .as_array()
            .unwrap()
            .clone()
    };
    let persona = |host: &Host, name: &str| {
        let all = personas(host);
        all.into_iter().find(|persona| persona["name"] == name)
    };
    let before = personas(&host).len();
    let made = |body: Value| host.ask("POST", "/agents", Some(&body));

    // `auto_contact` on a tool of Claude's tier: of the built-in contacts only Clarp is bound to
    // no tool, and it is the pool's first; it is taken, and nothing is minted.
    let (status, clarp) = made(json!({ "backend": "scripted", "auto_contact": true }));
    assert_eq!((status, &clarp["name"]), (200, &json!("Clarp")), "{clarp}");
    assert_eq!(personas(&host).len(), before);
    // Then the pool is empty, and a contact is minted for the next: the tier's first name, a
    // voice nobody speaks with and the tier's way of speaking. The apps are not told the
    // personas changed; only that an agent is there.
    let (status, first) = made(json!({ "backend": "scripted", "auto_contact": true }));
    assert_eq!(status, 200, "{first}");
    assert_eq!(first["name"], "Clara", "{first}");
    let clara = persona(&host, "Clara").expect("the minted contact is a persona");
    assert!(
        clara["voice_id"].as_str().unwrap().contains("cartesia"),
        "{clara}"
    );
    assert_eq!(
        clara["personality"],
        json!(clarp_kernel::contacts::personality("Clara", "claude")),
        "{clara}"
    );
    assert_eq!(first["agent"]["persona"], "Clara");
    assert_eq!(personas(&host).len(), before + 1);
    // `mint_contact` always mints, under a session of its own whatever it asked for.
    let asked =
        json!({ "backend": "scripted", "mint_contact": true, "name": "Given", "session": "given" });
    let (status, second) = made(asked);
    assert_eq!(status, 200, "{second}");
    assert_eq!(second["name"], "Ethan", "{second}");
    assert_ne!(second["session"], "given", "{second}");
    // A persona of no tier is in every tool's pool: `auto_contact` takes it, and mints nothing.
    let (status, kit) = host.ask("POST", "/personas", Some(&json!({ "name": "Kit" })));
    assert_eq!(status, 201, "{kit}");
    let (status, third) = made(json!({ "backend": "scripted", "auto_contact": true }));
    assert_eq!((status, &third["name"]), (200, &json!("Kit")), "{third}");
    assert_eq!(personas(&host).len(), before + 3);
    // Neither goes with another way of naming the agent, nor with the other.
    for asked in [
        json!({ "backend": "scripted", "mint_contact": true, "auto_contact": true }),
        json!({ "backend": "scripted", "auto_contact": true, "anonymous": true }),
        json!({ "backend": "scripted", "mint_contact": true, "replace_sid": first["session"] }),
    ] {
        let (status, said) = made(asked.clone());
        assert_eq!(
            (status, said),
            (
                400,
                json!({ "error": "conflicting launch options", "message": "conflicting launch options" })
            ),
            "{asked}"
        );
    }
    assert_eq!(personas(&host).len(), before + 3);

    // A running agent is given a contact, its conversation kept.
    let session = first["session"].as_str().unwrap().to_string();
    let conversation = host.get(&format!("/log?session={session}"))["conversation_id"].clone();
    let assign = |body: Value| host.ask("POST", "/agent-assign", Some(&body));
    let (status, options) = assign(json!({ "session": session, "mode": "options" }));
    assert_eq!(status, 200, "{options}");
    assert_eq!(
        options["contacts"],
        json!([]),
        "Clarp and Kit are worn, and the others are of other tools"
    );
    let (status, said) = assign(json!({ "session": session, "mode": "choose", "name": "kit" }));
    assert_eq!(status, 409, "{said}");
    assert_eq!(said["error"], "contact_unavailable");
    let (status, said) = assign(json!({ "session": session, "mode": "choose", "name": "Nobody" }));
    assert_eq!((status, &said["error"]), (404, &json!("contact not found")));
    let (status, said) = assign(json!({ "session": session, "mode": "anything" }));
    assert_eq!(
        (status, &said["error"]),
        (400, &json!("invalid assignment mode"))
    );
    let (status, said) = assign(json!({ "session": "nobody", "mode": "auto" }));
    assert_eq!((status, &said["error"]), (404, &json!("no such agent")));
    // Any persona nobody else goes by, of any tier.
    let (status, said) = assign(json!({ "session": session, "mode": "choose", "name": "mike" }));
    assert_eq!(status, 200, "{said}");
    assert_eq!(
        said,
        json!({ "ok": true, "session": session, "name": "Mike", "agent_id": first["agent"]["agent_id"] })
    );
    // `auto` where its own contact is of another tool and its pool is empty: one minted for it.
    let (status, said) = assign(json!({ "session": session, "mode": "auto" }));
    assert_eq!((status, &said["name"]), (200, &json!("Maya")), "{said}");
    // A new persona, made for another agent.
    let other = second["session"].as_str().unwrap();
    let (status, said) =
        assign(json!({ "session": other, "mode": "create", "name": "  New   Name " }));
    assert_eq!((status, &said["name"]), (200, &json!("New Name")), "{said}");
    let (status, said) = assign(json!({ "session": other, "mode": "create", "name": "kit" }));
    assert_eq!(
        (status, &said["error"]),
        (409, &json!("contact already exists"))
    );
    let row = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session.as_str())
        .cloned()
        .unwrap();
    assert_eq!(row["persona"], "Maya", "{row}");
    // It speaks as the contact speaks, with its voice.
    let kept: Value = {
        use clarp_ports::kv::KeyValueEngine as _;
        let store = clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap();
        let id = first["agent"]["agent_id"].as_str().unwrap();
        let rows = store.list("agent").unwrap();
        let (_, bytes) = rows.iter().find(|(key, _)| key == id).unwrap();
        serde_json::from_slice(bytes).unwrap()
    };
    assert_eq!(
        kept["personality"],
        json!(clarp_kernel::contacts::personality("Maya", "claude")),
        "{kept}"
    );
    let maya = persona(&host, "Maya").unwrap();
    assert_eq!(kept["voice_id"], maya["voice_id"], "{kept}");
    assert_eq!(
        host.get(&format!("/log?session={session}"))["conversation_id"],
        conversation
    );
    wait_for("the apps told of each contact assigned", || {
        let heard = events.lock().unwrap();
        let assigned: Vec<&Value> = heard
            .iter()
            .filter(|event| event["type"] == "agent-roster" && event["kind"] == "contact-assigned")
            .collect();
        assigned.len() == 3
            && assigned[1]["name"] == "Maya"
            && assigned[1]["session"] == session.as_str()
    });
    let heard = events.lock().unwrap();
    let created = heard
        .iter()
        .filter(|event| event["kind"] == "persona-created")
        .count();
    assert_eq!(
        created, 1,
        "only Kit, which the owner made, is told as made"
    );
    let renamed = heard
        .iter()
        .filter(|event| event["kind"] == "agent-renamed")
        .count();
    assert_eq!(
        renamed, 0,
        "a contact assigned is told as that, not as a new name"
    );
}

#[test]
fn a_contact_minted_for_a_launch_that_failed_is_left_unworn_and_the_next_launch_takes_it() {
    let place = Place::new("contacts-left");
    let host = place.start();
    let auto = |more: Value| {
        let mut asked = json!({ "backend": "claude", "auto_contact": true });
        asked
            .as_object_mut()
            .unwrap()
            .extend(more.as_object().unwrap().clone());
        host.ask("POST", "/agents", Some(&asked))
    };
    // Claude's pool is worn one contact after another, until a contact is minted.
    let mut worn = Vec::new();
    while !worn.contains(&json!("Clara")) {
        let (status, made) = auto(json!({}));
        assert_eq!(status, 200, "{made}");
        worn.push(made["name"].clone());
        assert!(worn.len() < 100, "nothing was minted: {worn:?}");
    }
    // A launch that fails behind the mint (a picture that is none): the contact minted for it
    // is written first, as the Python Host writes it, and stays, a persona nobody wears.
    let (status, said) = auto(json!({ "avatar_base64": "no picture at all!" }));
    assert_eq!(
        (status, &said["error"]),
        (400, &json!("invalid avatar")),
        "{said}"
    );
    let snapshot = host.get("/agents/snapshot");
    let listed = snapshot["personas"].as_array().unwrap();
    assert!(
        listed.iter().any(|persona| persona["name"] == "Ethan"),
        "{listed:?}"
    );
    let agents = snapshot["agents"].as_array().unwrap();
    assert!(
        agents.iter().all(|agent| agent["name"] != "Ethan"),
        "{agents:?}"
    );
    // It is what the pool is: the next launch takes it, and mints nothing.
    let before = listed.len();
    let (status, made) = auto(json!({}));
    assert_eq!((status, &made["name"]), (200, &json!("Ethan")), "{made}");
    let after = host.get("/agents/snapshot")["personas"]
        .as_array()
        .unwrap()
        .len();
    assert_eq!(after, before);
}

#[test]
fn launches_and_assignments_at_once_each_take_a_contact_of_their_own() {
    const AT_ONCE: usize = 8;
    let place = Place::new("contacts-at-once");
    let host = place.start();
    // Each asks at the same moment; the fleet chooses each contact in the turn that registers
    // or gives it, so none is refused for a contact another took, or a name another minted.
    let all_at_once = |asks: Vec<(String, Value)>| -> Vec<(u16, Value)> {
        std::thread::scope(|scope| {
            let asked: Vec<_> = asks
                .iter()
                .map(|(path, body)| scope.spawn(|| host.ask("POST", path, Some(body))))
                .collect();
            asked.into_iter().map(|one| one.join().unwrap()).collect()
        })
    };
    let distinct = |answers: &[(u16, Value)], what: &str| -> Vec<String> {
        let mut names: Vec<String> = answers
            .iter()
            .map(|(status, said)| {
                assert_eq!(*status, 200, "{what}: {said}");
                said["name"].as_str().unwrap().to_string()
            })
            .collect();
        names.sort();
        names.dedup();
        assert_eq!(names.len(), AT_ONCE, "{what}: {answers:?}");
        names
    };
    for (what, asked) in [
        (
            "auto_contact",
            json!({ "backend": "claude", "auto_contact": true }),
        ),
        (
            "mint_contact",
            json!({ "backend": "claude", "mint_contact": true }),
        ),
    ] {
        let asks = (0..AT_ONCE)
            .map(|_| ("/agents".to_string(), asked.clone()))
            .collect();
        distinct(&all_at_once(asks), what);
    }
    // Running agents of no contact of their own, each assigned one automatically at once: the
    // pool of their tool holds one free contact, and the others are minted, each its own.
    let sessions: Vec<String> = (0..AT_ONCE)
        .map(|n| {
            let made = host.post(
                "/agents",
                json!({ "name": format!("Worker {n}"), "backend": "scripted" }),
            );
            made["session"].as_str().unwrap().to_string()
        })
        .collect();
    let asks = sessions
        .iter()
        .map(|session| {
            (
                "/agent-assign".to_string(),
                json!({ "session": session, "mode": "auto" }),
            )
        })
        .collect();
    distinct(&all_at_once(asks), "agent-assign auto");
}
