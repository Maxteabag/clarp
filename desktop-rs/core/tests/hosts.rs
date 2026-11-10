use clarp_core::hosts::{self, MAX, SavedHost};
use clarp_core::settings::Settings;
use serde_json::json;

fn urls(settings: &Settings) -> Vec<String> {
    hosts::list(settings).into_iter().map(|h| h.url).collect()
}

#[test]
fn a_host_that_answered_is_remembered_most_recent_first() {
    let mut settings = Settings::in_memory();
    hosts::remember(&mut settings, "http://a:7682/", "Studio");
    hosts::remember(&mut settings, "https://b.example", "Laptop");
    assert_eq!(urls(&settings), ["https://b.example", "http://a:7682"], "trailing slash dropped, newest first");
    hosts::remember(&mut settings, "http://a:7682", "");
    assert_eq!(urls(&settings), ["http://a:7682", "https://b.example"], "reconnecting moves it to the front");
    assert_eq!(hosts::list(&settings)[0].name, "Studio", "an empty name keeps the saved one");
    hosts::remember(&mut settings, "http://a:7682", "Studio 2");
    assert_eq!(hosts::list(&settings)[0].name, "Studio 2");
}

#[test]
fn forgetting_a_host_removes_only_it() {
    let mut settings = Settings::in_memory();
    hosts::remember(&mut settings, "http://a:7682", "A");
    hosts::remember(&mut settings, "http://b:7682", "B");
    hosts::forget(&mut settings, "http://a:7682/");
    assert_eq!(urls(&settings), ["http://b:7682"]);
    hosts::forget(&mut settings, "http://missing");
    assert_eq!(urls(&settings), ["http://b:7682"]);
}

#[test]
fn the_list_is_capped_and_ignores_bad_rows() {
    let mut settings = Settings::in_memory();
    for i in 0..(MAX + 5) {
        hosts::remember(&mut settings, &format!("http://h{i}:1"), "");
    }
    assert_eq!(hosts::list(&settings).len(), MAX);
    assert_eq!(hosts::list(&settings)[0].url, format!("http://h{}:1", MAX + 4), "the newest is kept");
    settings.set(hosts::KEY, json!([{"url": ""}, {"name": "no url"}, 7, {"url": "http://x/"}, {"url": "http://x"}]));
    assert_eq!(hosts::list(&settings), [SavedHost { url: "http://x".into(), name: String::new() }]);
    hosts::remember(&mut settings, "  ", "nothing");
    assert_eq!(hosts::list(&settings).len(), 1, "an empty address is never saved");
}

#[test]
fn a_row_shows_the_name_else_the_address() {
    assert_eq!(SavedHost { url: "http://studio.tail:7682".into(), name: String::new() }.label(), "studio.tail:7682");
    assert_eq!(SavedHost { url: "https://clarp.example".into(), name: "Home".into() }.label(), "Home");
}
