//! Ports of tst_native_core::paneTreeSplitsClosesNavigatesAndZooms and
//! paneWorkspacePersistenceIsAsyncAndConflictSafe (the async half runs in the
//! Qt adapter; here writes are flushed synchronously).

use clarp_core::panes::{FileWorkspaceStore, PaneTree, WorkspaceStore};
use serde_json::{Value, json};

fn session_of(pane: &PaneTree) -> String {
    pane.active_session()
}

#[test]
fn pane_tree_splits_closes_navigates_and_zooms() {
    let mut panes = PaneTree::new();
    panes.set_active_session("rachel");
    let first = panes.active_pane_id().to_owned();
    assert_eq!((panes.pane_count(), session_of(&panes).as_str()), (1, "rachel"));

    panes.split_active("vertical", "bella");
    assert_eq!((panes.pane_count(), session_of(&panes).as_str()), (2, "bella"));
    assert_eq!(panes.root_node()["direction"], "vertical");
    panes.split_active("horizontal", "adam");
    assert_eq!((panes.pane_count(), session_of(&panes).as_str()), (3, "adam"));
    panes.navigate("left");
    assert_eq!(session_of(&panes), "rachel");

    // Complete a 2x2 grid. Directional movement follows the rendered
    // rectangles and stops at the outer edge rather than wrapping.
    panes.split_active("horizontal", "omar");
    assert_eq!((panes.pane_count(), session_of(&panes).as_str()), (4, "omar"));
    for (direction, expected) in [("right", "adam"), ("up", "bella"), ("left", "rachel"), ("left", "rachel")] {
        panes.navigate(direction);
        assert_eq!(session_of(&panes), expected, "after {direction}");
    }

    panes.toggle_zoom();
    assert_eq!(panes.zoomed_pane_id(), panes.active_pane_id());
    assert_eq!(panes.display_root()["kind"], "leaf");
    panes.navigate("down");
    assert_eq!(session_of(&panes), "omar");
    assert_eq!(panes.zoomed_pane_id(), panes.active_pane_id());
    panes.toggle_zoom();
    assert!(panes.zoomed_pane_id().is_empty());

    panes.close_pane(&first);
    assert_eq!(panes.pane_count(), 3);
    panes.equalize();
    panes.resize_active(0.1);
    let ratio = panes.root_node()["ratio"].as_f64().unwrap();
    assert!((0.15..=0.85).contains(&ratio));

    let mut grid = PaneTree::new();
    grid.set_active_session("root");
    grid.split_active("vertical", "right");
    grid.split_active("horizontal", "right-bottom");
    grid.navigate("left");
    grid.split_active("horizontal", "left-bottom");
    for pass in [("vertical", "column"), ("horizontal", "row")] {
        let ids: Vec<String> = grid.pane_layout().iter().map(|p| p["id"].as_str().unwrap().to_owned()).collect();
        for id in ids {
            grid.focus_pane(&id);
            grid.split_active(pass.0, pass.1);
        }
    }
    assert_eq!(grid.pane_count(), 16);
    let pane_at = |grid: &PaneTree, column: f64, row: f64| {
        let (tx, ty) = ((column + 0.5) / 4.0, (row + 0.5) / 4.0);
        grid.pane_layout()
            .iter()
            .map(|p| {
                let cx = p["x"].as_f64().unwrap() + p["width"].as_f64().unwrap() / 2.0;
                let cy = p["y"].as_f64().unwrap() + p["height"].as_f64().unwrap() / 2.0;
                ((cx - tx).abs() + (cy - ty).abs(), p["id"].as_str().unwrap().to_owned())
            })
            .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap())
            .unwrap()
            .1
    };
    let start = pane_at(&grid, 1.0, 1.0);
    grid.focus_pane(&start);
    for (direction, column, row) in [("right", 2.0, 1.0), ("down", 2.0, 2.0), ("left", 1.0, 2.0)] {
        grid.navigate(direction);
        assert_eq!(grid.active_pane_id(), pane_at(&grid, column, row), "after {direction}");
    }

    let mut irregular = PaneTree::new();
    irregular.set_active_session("top-left");
    irregular.split_active("horizontal", "bottom");
    let bottom = irregular.active_pane_id().to_owned();
    irregular.navigate("up");
    irregular.split_active("vertical", "top-right");
    irregular.focus_pane(&bottom);
    irregular.navigate("right");
    assert_eq!(irregular.active_pane_id(), bottom);
    irregular.navigate("left");
    assert_eq!(irregular.active_pane_id(), bottom);
}

#[test]
fn prev_and_next_cycle_and_workspaces_switch() {
    let mut panes = PaneTree::new();
    panes.set_active_session("a");
    panes.split_active("vertical", "b");
    panes.navigate("next");
    assert_eq!(panes.active_session(), "a");
    panes.navigate("prev");
    assert_eq!(panes.active_session(), "b");

    let main = panes.active_workspace().to_owned();
    panes.create_workspace("  Review  ");
    let review = panes.active_workspace().to_owned();
    assert_eq!(panes.pane_count(), 1);
    assert!(panes.workspaces().iter().any(|w| w["name"] == "Review"));
    // Hidden workspaces keep their panes in the view layout, not shown.
    let hidden = panes.view_layout().iter().filter(|p| p["shown"] == false).count();
    assert_eq!(hidden, 2);
    panes.switch_workspace(&main);
    assert_eq!(panes.pane_count(), 2);
    assert_eq!(panes.active_session(), "b");
    panes.move_active_to_workspace(&review);
    assert_eq!(panes.active_workspace(), review);
    assert_eq!(panes.active_session(), "b");
    assert_eq!(panes.pane_count(), 2, "moved into the review workspace's split");
}

#[test]
fn a_workspace_closes_to_its_neighbour_but_the_last_stays() {
    let mut panes = PaneTree::new();
    panes.set_active_session("a");
    let main = panes.active_workspace().to_owned();
    panes.close_workspace(&main);
    assert_eq!(panes.workspaces().len(), 1, "the last workspace stays");
    panes.create_workspace("Review");
    let review = panes.active_workspace().to_owned();
    panes.set_active_session("b");
    panes.split_active("vertical", "c");
    panes.switch_workspace(&main);
    // Closing a hidden one leaves the active alone.
    panes.close_workspace(&review);
    assert_eq!(panes.workspaces().len(), 1);
    assert_eq!(panes.active_workspace(), main);
    assert_eq!(panes.active_session(), "a");
    // Closing the active one opens a neighbour with its layout.
    panes.create_workspace("Second");
    let second = panes.active_workspace().to_owned();
    panes.close_workspace(&second);
    assert_eq!(panes.workspaces().len(), 1);
    assert_eq!(panes.active_workspace(), main);
    assert_eq!(panes.active_session(), "a");
    assert!(panes.view_layout().iter().all(|p| p["shown"] == true), "no hidden panes of a closed workspace remain");
}

#[test]
fn saved_state_round_trips_and_rejects_malformed_trees() {
    let mut panes = PaneTree::new();
    panes.set_active_session("a");
    panes.split_active("horizontal", "b");
    panes.toggle_zoom();
    let state = panes.save_state();
    let mut restored = PaneTree::new();
    assert!(restored.load_state(&state));
    assert_eq!(restored.root_node(), panes.root_node());
    assert_eq!(restored.zoomed_pane_id(), panes.zoomed_pane_id());
    // New ids continue past the restored ones.
    restored.split_active("vertical", "c");
    assert!(!state["root"].to_string().contains(restored.active_pane_id()));

    let bad = |root: Value, active: &str| {
        json!({"root": root, "activePaneId": active}).as_object().cloned().unwrap()
    };
    let mut target = PaneTree::new();
    assert!(!target.load_state(&bad(json!({"id": "nope", "kind": "leaf"}), "nope")));
    assert!(!target.load_state(&bad(json!({"id": "pane-1", "kind": "leaf"}), "pane-2")));
    let duplicate = json!({"id": "split-1", "kind": "split", "first": {"id": "pane-1", "kind": "leaf"},
                           "second": {"id": "pane-1", "kind": "leaf"}});
    assert!(!target.load_state(&bad(duplicate, "pane-1")));
    let mut deep = json!({"id": "pane-99", "kind": "leaf"});
    for i in 0..9 {
        deep = json!({"id": format!("split-{i}"), "kind": "split", "first": deep, "second": {"id": format!("pane-{}", 50 + i), "kind": "leaf"}});
    }
    assert!(!target.load_state(&bad(deep, "pane-99")), "deeper than 8 levels is rejected");
}

fn temp_store() -> (FileWorkspaceStore, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("clarp-panes-{}", uuid_like()));
    (FileWorkspaceStore::new(dir.join("workspaces.json")), dir)
}

/// Unique per test: macOS's clock moves in microseconds, so two tests
/// starting together got one folder and one's cleanup removed the other's.
fn uuid_like() -> String {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("{}-{}-{n}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos())
}

fn saved_session(encoded: &str) -> String {
    let document: Value = serde_json::from_str(encoded).unwrap_or(Value::Null);
    let active = document["active"].as_str().unwrap_or_default().to_owned();
    document["states"][active]["root"]["session"].as_str().unwrap_or_default().to_owned()
}

#[test]
fn pane_workspace_persistence_is_conflict_safe() {
    let (store, dir) = temp_store();
    {
        let mut first = PaneTree::persisted(&store, true);
        first.set_active_session("scoped-alpha");
        first.create_workspace("Scoped review");
        first.set_active_session("scoped-beta");
        first.flush_writes(&store);
    }
    {
        let second = PaneTree::persisted(&store, true);
        assert_eq!(second.workspaces().len(), 2);
        assert_eq!(saved_session(&store.read_collection()), "scoped-beta");
        assert_eq!(second.active_session(), "scoped-beta");
        let empty = PaneTree::persisted(&store, false);
        assert_eq!(empty.workspaces().len(), 1, "an empty startup does not restore");
    }
    std::fs::remove_dir_all(&dir).unwrap();

    let mut panes = PaneTree::persisted(&store, true);
    panes.set_active_session("alpha");
    panes.flush_writes(&store);
    assert_eq!(saved_session(&store.read_collection()), "alpha");
    assert!(panes.workspace_save_warning().is_empty());

    let external = json!({"version": 1, "active": "workspace-1", "names": {"workspace-1": "Other window"},
        "states": {"workspace-1": {"activePaneId": "pane-1",
            "root": {"id": "pane-1", "kind": "leaf", "session": "external"}}}}).to_string();
    store.set_collection(&external).unwrap();

    panes.set_active_session("beta");
    panes.flush_writes(&store);
    assert!(panes.workspace_save_warning().contains("recovery"), "{}", panes.workspace_save_warning());
    assert!(panes.take_signals().contains(&clarp_core::panes::Signal::WorkspaceSaveWarningChanged));
    assert_eq!(saved_session(&store.read_collection()), "external");
    let recovery = store.recovery();
    assert_eq!(recovery.len(), 1);
    assert_eq!(saved_session(recovery.values().next().unwrap().as_str().unwrap()), "beta");

    panes.save_workspace_layout_instead();
    panes.flush_writes(&store);
    assert_eq!(saved_session(&store.read_collection()), "beta");
    assert!(panes.workspace_save_warning().is_empty());
    assert!(store.recovery().is_empty(), "a successful save drops the recovery copy");

    // A held lock keeps the layout in recovery instead of overwriting.
    let lock = dir.join("workspaces.json.workspace-save.lock");
    std::fs::write(&lock, "").unwrap();
    panes.set_active_session("gamma");
    panes.flush_writes(&store);
    assert_eq!(saved_session(&store.read_collection()), "beta");
    assert!(panes.workspace_save_warning().contains("recovery"));
    assert_eq!(saved_session(store.recovery().values().next().unwrap().as_str().unwrap()), "gamma");
    std::fs::remove_file(&lock).unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_queued_layout_replaces_an_older_one_while_a_write_is_in_flight() {
    let (store, dir) = temp_store();
    let mut panes = PaneTree::persisted(&store, true);
    panes.set_active_session("one");
    let first = panes.next_write().expect("a write is queued");
    assert!(panes.next_write().is_none(), "one write at a time");
    panes.set_active_session("two");
    panes.set_active_session("three");
    let result = store.write(&first);
    panes.finish_write(&result);
    let second = panes.next_write().expect("the newest layout is queued");
    assert_eq!(saved_session(&second.encoded), "three");
    assert_eq!(second.expected_collection, first.encoded, "expects what it just wrote");
    panes.finish_write(&store.write(&second));
    assert_eq!(saved_session(&store.read_collection()), "three");
    assert!(panes.workspace_save_warning().is_empty());
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn dismissing_a_layout_conflict_accepts_the_newer_layout_and_saves_normally_after() {
    let (store, dir) = temp_store();
    let mut panes = PaneTree::persisted(&store, true);
    panes.set_active_session("alpha");
    panes.flush_writes(&store);

    // Another window saves while this one is open (two windows for a while).
    let external = json!({"version": 1, "active": "workspace-1", "names": {"workspace-1": "Other window"},
        "states": {"workspace-1": {"activePaneId": "pane-1",
            "root": {"id": "pane-1", "kind": "leaf", "session": "external"}}}}).to_string();
    store.set_collection(&external).unwrap();
    panes.set_active_session("beta");
    panes.flush_writes(&store);
    assert!(!panes.workspace_save_warning().is_empty(), "the conflict is reported");
    panes.take_signals();

    // Dismissed: the warning goes, the other window's layout stays saved.
    panes.dismiss_workspace_save_warning();
    assert!(panes.workspace_save_warning().is_empty(), "{}", panes.workspace_save_warning());
    assert!(panes.take_signals().contains(&clarp_core::panes::Signal::WorkspaceSaveWarningChanged));
    panes.flush_writes(&store);
    assert_eq!(saved_session(&store.read_collection()), "external", "dismissing overwrites nothing");

    // The other window is gone: this window's next change saves as usual.
    panes.set_active_session("gamma");
    panes.flush_writes(&store);
    assert!(panes.workspace_save_warning().is_empty(), "no new conflict: {}", panes.workspace_save_warning());
    assert_eq!(saved_session(&store.read_collection()), "gamma");
    std::fs::remove_dir_all(&dir).unwrap();
}
