use std::time::{Duration, Instant};

use clarp_core::update_feeds::{
    Gate, Intervals, Patch, Throttle, artifact_item_path, artifact_list_path, flatten, is_unsupported_representation, listed,
    patch_artifact,
};
use serde_json::{Value, json};

const SECOND: Duration = Duration::from_secs(1);

#[test]
fn the_first_request_goes_at_once_and_later_ones_wait_out_the_interval() {
    let t0 = Instant::now();
    let mut throttle = Throttle::new(30 * SECOND);
    assert_eq!(throttle.request(t0), Gate::Now);
    throttle.started(t0);
    // Ten events in the next seconds arm one trailing reload, due when the
    // interval since the last one ends.
    assert_eq!(throttle.request(t0 + SECOND), Gate::After(29 * SECOND));
    for n in 2..12 {
        assert_eq!(throttle.request(t0 + n * SECOND), Gate::Armed);
    }
    assert!(throttle.fire(), "the trailing reload is owed");
    throttle.started(t0 + 30 * SECOND);
    assert!(!throttle.fire(), "and only once");
    // After a quiet interval an event goes at once again.
    assert_eq!(throttle.request(t0 + 61 * SECOND), Gate::Now);
}

#[test]
fn a_reload_for_another_reason_satisfies_the_armed_one() {
    let t0 = Instant::now();
    let mut throttle = Throttle::new(5 * SECOND);
    throttle.started(t0);
    assert_eq!(throttle.request(t0 + SECOND), Gate::After(4 * SECOND));
    // The user refreshed meanwhile.
    throttle.started(t0 + 2 * SECOND);
    assert!(!throttle.fire(), "the refresh already read it");
    assert_eq!(throttle.request(t0 + 3 * SECOND), Gate::After(4 * SECOND), "the interval counts from the refresh");
}

#[test]
fn under_continuous_events_reloads_are_one_interval_apart() {
    let t0 = Instant::now();
    let mut throttle = Throttle::new(30 * SECOND);
    let mut reloads = Vec::new();
    let mut due: Option<Instant> = None;
    // An event every second for ten minutes; a timer fires when due.
    for second in 0..600 {
        let now = t0 + second * SECOND;
        if due.is_some_and(|at| at <= now) {
            due = None;
            if throttle.fire() {
                throttle.started(now);
                reloads.push(second);
            }
        }
        match throttle.request(now) {
            Gate::Now => {
                throttle.started(now);
                reloads.push(second);
            }
            Gate::After(delay) => due = Some(now + delay),
            Gate::Armed => {}
        }
    }
    assert_eq!(reloads.len(), 20, "{reloads:?}");
    assert!(reloads.windows(2).all(|w| w[1] - w[0] == 30), "{reloads:?}");
}

#[test]
fn the_default_intervals_follow_the_incident() {
    let intervals = Intervals::default();
    assert!(intervals.artifacts >= 15 * SECOND && intervals.artifacts <= 30 * SECOND);
    assert_eq!(intervals.attention, 5 * SECOND);
    assert!(intervals.jobs < intervals.attention);
}

#[test]
fn the_paths_ask_for_flat_v1_unless_the_host_refused_it() {
    assert_eq!(artifact_list_path(true), "/artifacts?limit=50&order=updated&representation=flat-v1");
    assert_eq!(artifact_list_path(false), "/artifacts?limit=50&order=updated");
    assert_eq!(artifact_item_path("a b/c", true), "/artifacts/a%20b%2Fc?representation=flat-v1");
    assert_eq!(artifact_item_path("x", false), "/artifacts/x");
    assert!(is_unsupported_representation(400, "unsupported artifact representation (HTTP 400)"));
    assert!(!is_unsupported_representation(400, "invalid limit"));
    assert!(!is_unsupported_representation(500, "unsupported artifact representation"));
}

#[test]
fn flat_v1_drops_only_the_nested_payload() {
    let mut item = json!({"artifact_id": "f", "type": "html_form", "content": "<html/>", "read_only": true,
                          "connect_origins": ["https://a.example"], "version": 3,
                          "payload": {"content": "<html/>", "read_only": true, "connect_origins": ["https://a.example"], "version": 3}});
    flatten(&mut item);
    assert_eq!(item, json!({"artifact_id": "f", "type": "html_form", "content": "<html/>", "read_only": true,
                            "connect_origins": ["https://a.example"], "version": 3}));
}

fn item(id: &str, updated_at: i64) -> Value {
    json!({"artifact_id": id, "type": "document", "title": id, "updated_at": updated_at})
}

fn ids(list: &[Value]) -> Vec<&str> {
    list.iter().map(|a| a["artifact_id"].as_str().unwrap()).collect()
}

#[test]
fn an_updated_item_moves_to_its_place_and_a_new_one_joins() {
    let mut list = vec![item("c", 30), item("b", 20), item("a", 10)];
    assert_eq!(patch_artifact(&mut list, "a", Some(item("a", 40)), 50), Patch::Placed { overflow: false });
    assert_eq!(ids(&list), ["a", "c", "b"]);
    assert_eq!(patch_artifact(&mut list, "d", Some(item("d", 25)), 50), Patch::Placed { overflow: false });
    assert_eq!(ids(&list), ["a", "c", "d", "b"]);
    // Same time: the higher id first, as the Host's ORDER BY.
    assert_eq!(patch_artifact(&mut list, "e", Some(item("e", 30)), 50), Patch::Placed { overflow: false });
    assert_eq!(ids(&list), ["a", "e", "c", "d", "b"]);
}

#[test]
fn a_change_without_a_new_time_stays_in_place() {
    let mut list = vec![item("c", 30), item("b", 20), item("a", 10)];
    let mut pinned = item("b", 20);
    pinned["pinned"] = json!(true);
    patch_artifact(&mut list, "b", Some(pinned), 50);
    assert_eq!(ids(&list), ["c", "b", "a"]);
    assert_eq!(list[1]["pinned"], json!(true));
}

#[test]
fn deleted_and_unlisted_items_leave_and_a_full_list_asks_for_a_reload() {
    let mut list = vec![item("c", 30), item("b", 20), item("a", 10)];
    assert_eq!(patch_artifact(&mut list, "b", None, 50), Patch::Removed { was_full: false });
    let mut deleted = item("c", 31);
    deleted["deleted_at"] = json!(31);
    assert_eq!(patch_artifact(&mut list, "c", Some(deleted), 2), Patch::Removed { was_full: true });
    assert!(Patch::Removed { was_full: true }.needs_reload());
    assert_eq!(ids(&list), ["a"]);
    let mut image = item("i", 50);
    image["type"] = json!("image");
    assert!(!listed(&image));
    assert_eq!(patch_artifact(&mut list, "i", Some(image), 50), Patch::Unchanged);
    assert_eq!(patch_artifact(&mut list, "zz", None, 50), Patch::Unchanged);
    assert_eq!(ids(&list), ["a"]);
}

#[test]
fn an_archived_item_stays_listed_as_the_host_lists_it() {
    let mut list = vec![item("b", 20), item("a", 10)];
    let mut archived = item("a", 40);
    archived["archived_at"] = json!(40);
    patch_artifact(&mut list, "a", Some(archived), 50);
    assert_eq!(ids(&list), ["a", "b"]);
}

#[test]
fn a_new_item_past_a_full_list_overflows_and_asks_for_a_reload() {
    let mut list = vec![item("b", 20), item("a", 10)];
    let patch = patch_artifact(&mut list, "c", Some(item("c", 30)), 2);
    assert_eq!(patch, Patch::Placed { overflow: true });
    assert!(patch.needs_reload());
    assert_eq!(ids(&list), ["c", "b", "a"], "kept until the reload says which left");
}
