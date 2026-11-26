use super::*;

/// Issue 881: a page of `/log` asked with a budget holds at most that many bytes of rows, a row
/// over its size comes as a placeholder that says where to read it whole, and a client that
/// asked for no budget is answered as before.
#[test]
fn a_page_with_a_budget_is_bounded_in_bytes_and_a_large_row_is_fetched_on_its_own() {
    let place = Place::new("log-budget");
    let host = place.start();
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features.as_array().unwrap().contains(&json!("log_budget")),
        "{features}"
    );
    let agent = host.new_agent("Budget");
    let large = format!("large {}", "word ".repeat(8_000));
    let says = [
        "small one".to_string(),
        large,
        "small two".to_string(),
        "small three".to_string(),
    ];
    for (n, text) in says.iter().enumerate() {
        host.send(&agent, text, &format!("b{n}"));
        wait_for("the echo", || {
            host.turns(&agent)
                .iter()
                .filter(|row| row["role"] == "assistant")
                .count()
                > n
                && host.state(&agent) != "thinking"
        });
    }
    let page = |query: &str| host.get(&format!("/log?session={agent}&limit=100{query}"));
    let rows_of = |page: &Value| page["turns"].as_array().unwrap().clone();

    // Without a budget: every row whole, as before.
    let whole = page("");
    assert!(whole.get("budget").is_none());
    assert!(rows_of(&whole).len() >= 8, "{whole:#}");
    assert!(
        rows_of(&whole)
            .iter()
            .all(|row| row.get("placeholder").is_none())
    );
    let size = |rows: &[Value]| {
        rows.iter()
            .map(|row| row.to_string().len() + 1)
            .sum::<usize>()
    };
    assert!(size(&rows_of(&whole)) > 80_000);

    // With one: the two large rows are placeholders, and the page holds every row.
    let bounded = page("&max_bytes=16384");
    assert_eq!(
        bounded["budget"],
        json!({"max_bytes": 16384, "row_max": 8192, "cut": false, "placeholders": 2})
    );
    let rows = rows_of(&bounded);
    assert_eq!(rows.len(), rows_of(&whole).len());
    assert!(size(&rows) <= 16_384, "{}", size(&rows));
    let held: Vec<&Value> = rows
        .iter()
        .filter(|row| row.get("placeholder").is_some())
        .collect();
    assert_eq!(held.len(), 2);
    for row in &held {
        let placeholder = &row["placeholder"];
        assert_eq!(row["text"], "");
        assert!(
            placeholder["bytes"].as_u64().unwrap() > 40_000,
            "{placeholder}"
        );
        let had = rows_of(&whole)
            .into_iter()
            .find(|had| had["id"] == row["id"])
            .unwrap();
        assert_eq!(
            placeholder["text_chars"],
            json!(had["text"].as_str().unwrap().chars().count())
        );
        assert!(
            placeholder["preview"]
                .as_str()
                .unwrap()
                .starts_with("large word word")
        );
        assert_eq!(
            placeholder["preview"].as_str().unwrap().chars().count(),
            280
        );
        assert_eq!(
            placeholder["fetch"],
            json!(format!(
                "/log?session={agent}&message_id={}",
                row["id"].as_str().unwrap()
            ))
        );
        assert!(row["revision"].as_u64().is_some() && row["role"].is_string());
        // What it says to fetch answers the row whole, as the page without a budget had it.
        let fetched = host.get(placeholder["fetch"].as_str().unwrap());
        let one = rows_of(&fetched);
        assert_eq!(one.len(), 1);
        assert_eq!(one[0]["text"], had["text"]);
        assert_eq!(one[0]["revision"], had["revision"]);
        assert_eq!(fetched["has_more"], false);
    }
    let (status, _) = host.ask(
        "GET",
        &format!("/log?session={agent}&message_id=nothing-of-it"),
        None,
    );
    assert_eq!(status, 404);

    // A smaller budget cuts the newest page to its newest rows; the older are still to read.
    let tight = page("&max_bytes=16384&row_max=1024");
    let kept = rows_of(&tight);
    assert!(size(&kept) <= 16_384);
    assert_eq!(tight["budget"]["placeholders"], 2);
    let tiny = page("&max_bytes=1&row_max=1024");
    assert_eq!(
        tiny["budget"]["max_bytes"], 16384,
        "a budget is at least 16 KB"
    );

    // Read forward with a budget, a client reaches the head page by page.
    let mut cursor = 0;
    let mut seen = Vec::new();
    let first = page("&max_bytes=65536&row_max=60000");
    assert_eq!(first["budget"]["cut"], true);
    assert_eq!(
        first["has_more"], true,
        "the newest page cut leaves older rows"
    );
    assert_eq!(
        rows_of(&first).last().unwrap()["id"],
        rows_of(&whole).last().unwrap()["id"]
    );
    for _ in 0..20 {
        let next = page(&format!(
            "&max_bytes=65536&row_max=60000&after_revision={}",
            cursor.max(1)
        ));
        for row in rows_of(&next) {
            assert!(row.to_string().len() <= 60_000);
            seen.push(row["id"].as_str().unwrap().to_string());
        }
        assert!(size(&rows_of(&next)) <= 65_536 || rows_of(&next).len() == 1);
        cursor = next["latest_revision"].as_u64().unwrap();
        if !next["has_more"].as_bool().unwrap() {
            break;
        }
    }
    assert_eq!(cursor, whole["latest_revision"].as_u64().unwrap());
    for row in rows_of(&whole) {
        if row["revision"].as_u64().unwrap() > 1 {
            assert!(
                seen.contains(&row["id"].as_str().unwrap().to_string()),
                "{} was read",
                row["id"]
            );
        }
    }
    let (status, refused) = host.ask("GET", &format!("/log?session={agent}&max_bytes=lots"), None);
    assert_eq!(
        (status, &refused["error"]),
        (400, &json!("max_bytes must be a number of bytes"))
    );
}
