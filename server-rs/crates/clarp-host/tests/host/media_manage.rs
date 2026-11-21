use super::*;

#[test]
fn gallery_changes_are_durable_and_only_real_changes_emit_events() {
    let place = Place::new("media-manage");
    let admin = "test-administrator";
    let host = place.start_with_token(admin);
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["media_manage"],
        113
    );
    let session = host.new_agent("Gallery owner");
    let (phone, _) = pair(&host, "Gallery reader", "limited");
    let heard = host.listen_at(&format!("/events?token={admin}"));
    let png = [
        b"\x89PNG\r\n\x1a\n".as_slice(),
        b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00",
    ]
    .concat();
    let publish = |host: &Host, mime, bytes: &[u8], name| -> String {
        let response = fetch(
            host,
            admin,
            "POST",
            "/media",
            &[
                ("X-Session", session.as_str()),
                ("Content-Type", mime),
                ("X-File-Name", name),
            ],
            bytes,
        );
        assert_eq!(response.status, 200, "{}", response.text());
        let answer: Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(answer["favorite"], false);
        assert!(answer["favorite_at"].is_null());
        answer["asset_id"].as_str().unwrap().to_owned()
    };
    let first = publish(&host, "image/png", &png, "first.png");
    let second = publish(&host, "image/png", &png, "second.png");
    let text = publish(&host, "text/plain", b"note", "note.txt");
    let file = format!("/media/{first}");
    let mark = format!("{file}/favorite");
    let list = format!("/media?session={session}");
    let bookmark = json!({"favorite": true});
    assert_eq!(host.ask_as(&phone, "GET", &list, None).0, 200);
    assert_eq!(host.ask_as(&phone, "POST", &mark, Some(&bookmark)).0, 403);
    assert_eq!(host.ask_as(&phone, "DELETE", &file, None).0, 403);
    let turn = fetch(
        &host,
        admin,
        "DELETE",
        &file,
        &[("X-Clarp-Turn", "fixture-turn")],
        b"",
    );
    assert_eq!(turn.status, 403);
    assert_eq!(
        serde_json::from_slice::<Value>(&turn.body).unwrap()["error"],
        "agents_may_not"
    );
    assert_eq!(
        host.ask("POST", &mark, Some(&json!({"favorite": "true"})))
            .0,
        400
    );
    assert_eq!(host.ask("DELETE", &format!("/media/{text}"), None).0, 422);
    assert_eq!(host.ask("DELETE", "/media/unknown", None).0, 404);

    let portrait = host.post(
        "/agent-portraits",
        json!({
            "session": session, "action": "add_media_asset", "asset_id": first,
        }),
    );
    let marked = host.post(&mark, bookmark.clone());
    assert_eq!(marked["favorite"], true);
    assert!(marked["favorite_at"].is_number());
    assert_eq!(marked["used_as"][0]["kind"], "portrait");
    assert_eq!(host.post(&mark, bookmark.clone()), marked);
    let favorites = host.get(&format!("{list}&favorites=1"));
    assert_eq!(favorites["total"], 2);
    assert_eq!(favorites["favorites_total"], 1);
    assert_eq!(favorites["assets"][0], marked);
    let first_page = host.get(&format!("{list}&limit=1"));
    let before = first_page["next_before"].as_str().unwrap();
    let second_page = host.get(&format!("{list}&limit=1&before={before}"));
    assert_ne!(
        first_page["assets"][0]["asset_id"],
        second_page["assets"][0]["asset_id"]
    );
    assert!(second_page["next_before"].is_null());

    for rejected in [
        json!({}),
        json!({"asset_ids": []}),
        json!({"asset_ids": [""]}),
        json!({"asset_ids": [true]}),
        json!({"asset_ids": vec![first.clone(); 201]}),
    ] {
        assert_eq!(host.ask("POST", "/media/delete", Some(&rejected)).0, 400);
    }
    assert_eq!(host.ask("GET", "/media/deleted", None).0, 400);
    assert_eq!(host.ask("GET", "/media/deleted?since=-1", None).0, 400);
    assert_eq!(
        host.ask("GET", "/media/deleted?since=0&limit=bad", None).0,
        400
    );
    let limit_error = json!({"error": "limit must be a number from 1 to 1000"});
    for limit in ["0", "-1", "1001", "1.5", "%20", "&limit=0"] {
        assert_eq!(
            host.ask(
                "GET",
                &format!("/media/deleted?since=0&limit={limit}"),
                None
            ),
            (400, limit_error.clone()),
            "{limit}"
        );
    }
    for limit in [
        "",
        "1",
        "1000",
        "%201%20",
        "%2B1",
        "1_000",
        "%D9%A1",
        "&limit=1",
        "1&limit=0",
    ] {
        let path = format!("/media/deleted?since=0&limit={limit}");
        assert_eq!(host.ask("GET", &path, None).0, 200, "{path}");
    }
    let batch = host.post(
        "/media/delete",
        json!({"asset_ids": [first, "unknown", text, first]}),
    );
    let good = &batch["results"][0];
    assert_eq!(good["deleted"], true);
    assert_eq!(good["still_used_as"], marked["used_as"]);
    assert_eq!(&batch["results"][3], good);
    assert_eq!(batch["results"][1]["error"], "not_found");
    assert_eq!(batch["results"][2]["error"], "not_an_image");
    let gone = json!({"error": "removed", "asset_id": first, "deleted_at": good["deleted_at"]});
    assert_eq!(host.ask("GET", &file, None), (410, gone.clone()));
    assert_eq!(
        host.ask("POST", &mark, Some(&bookmark)),
        (410, gone.clone())
    );
    let range = fetch(&host, admin, "GET", &file, &[("Range", "bytes=0-2")], b"");
    assert_eq!(range.status, 410);
    let remaining = host.get(&list);
    assert_eq!(remaining["total"], 1);
    assert_eq!(remaining["favorites_total"], 0);
    assert_eq!(remaining["assets"][0]["asset_id"], second);
    assert_eq!(
        fetch(&host, admin, "GET", &format!("/media/{second}"), &[], b"").body,
        png
    );
    assert_eq!(
        host.get(&format!("/agent-portraits?session={session}"))["portraits"],
        portrait["portraits"]
    );
    let deleted = host.ask_as(
        &phone,
        "GET",
        &format!("/media/deleted?since=0&session={session}"),
        None,
    );
    assert_eq!(deleted.0, 200);
    assert_eq!(
        deleted.1["deleted"],
        json!([{
            "asset_id": first, "session": session, "deleted_at": good["deleted_at"],
        }])
    );
    assert!(deleted.1["next_since"].is_null());
    let events = || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["type"] == "media-changed")
            .cloned()
            .collect::<Vec<_>>()
    };
    wait_for("gallery events to arrive", || events().len() >= 4);
    let events = events();
    assert_eq!(events.len(), 4, "{events:?}");
    assert_eq!(events[0]["change"], "created");
    assert_eq!(events[2]["change"], "favorite");
    assert_eq!(events[2]["favorite"], true);
    assert_eq!(events[3]["change"], "deleted");
    assert!(events[3].get("favorite").is_none());
    assert_eq!(events[3]["at"], good["deleted_at"]);
    drop(host);

    let host = place.start_with_token(admin);
    assert_eq!(host.ask("GET", &file, None), (410, gone));
    assert_eq!(host.ask("DELETE", &file, None), (200, good.clone()));
    let replacement = publish(&host, "image/png", &png, "replacement.png");
    assert_ne!(replacement, first);
    assert_eq!(
        fetch(
            &host,
            admin,
            "GET",
            &format!("/media/{replacement}"),
            &[],
            b""
        )
        .body,
        png
    );
}
