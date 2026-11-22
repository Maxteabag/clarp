//! `--check render-spec --out DIR`: a native form (render_spec, issue 428)
//! published in a chat is drawn in its card under the reply that made it,
//! as iOS draws it. Send waits for the required answers, the answer is
//! posted through the form path, and the form is read-only afterwards and
//! again after the window forgets every form and reads it from the Host.
//! The desktop names itself in `X-Clarp-Client`, and the fake Host draws the
//! kind only for the names its table admits, as the real one does.
//!
//! Then a catalog version 2 form (issue 673): its line, bar and pie charts
//! and its table drawn with numbers in the device's decimal separator and
//! never grouped, its two buttons side by side and no footer submit. Every
//! button waits for the required note; Hold asks its question first and
//! Cancel sends nothing; Ship it sends once with intent `button` and its
//! action, the chosen button is marked and every button is off, and the
//! badge reads "Answered · Ship it" (from the row's `answered_label` after
//! a reload too). A Host that names the desktop for version 1 only gives it
//! the v2 form as the plain card while the v1 form stays drawn.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, check, control, posts, requests, rows, run_stages, shot};
use crate::{ArtifactBridge, ArtifactItem};

const ID: &str = "rs-release";
const SUBMIT: &str = "/artifacts/rs-release/submit";
const HEADER: &str = "desktop/0.1.0 contract=2";
const V2: &str = "rs-deploy";
const V2_SUBMIT: &str = "/artifacts/rs-deploy/submit";

fn card_of(window: &crate::AppWindow, id: &str) -> Option<(String, ArtifactItem)> {
    rows(window).iter().flat_map(|row| row.artifacts.iter().map(|a| (row.id.to_string(), a)).collect::<Vec<_>>()).find(|(_, a)| a.id == id)
}

fn card(window: &crate::AppWindow) -> Option<(String, ArtifactItem)> {
    card_of(window, ID)
}

fn form(window: &crate::AppWindow) -> Option<ArtifactItem> {
    card(window).map(|(_, a)| a).filter(|a| a.spec_state == "form")
}

fn v2_form(window: &crate::AppWindow) -> Option<ArtifactItem> {
    card_of(window, V2).map(|(_, a)| a).filter(|a| a.spec_state == "form")
}

/// The form's rows as drawn: a leaf's kind, or "cells" for elements side by side.
fn kinds(item: &ArtifactItem) -> Vec<String> {
    item.spec_rows.iter().map(|r| if r.cells.row_count() > 0 { "cells".into() } else { r.leaf.kind.to_string() }).collect()
}

/// The buttons of the v2 form's button row: (label, action, enabled, chosen, busy).
fn buttons(item: &ArtifactItem) -> Vec<(String, String, bool, bool, bool)> {
    item.spec_rows
        .iter()
        .flat_map(|r| r.cells.iter().collect::<Vec<_>>())
        .filter(|l| l.kind == "button")
        .map(|l| (l.text.to_string(), l.key.to_string(), l.enabled, l.on, l.busy))
        .collect()
}

fn texts(model: &slint::ModelRc<crate::FormChartTick>) -> Vec<String> {
    model.iter().map(|t| t.text.to_string()).collect()
}

fn bridge(window: &crate::AppWindow) -> ArtifactBridge<'_> {
    window.global::<ArtifactBridge>()
}

/// The values a form row shows, by its answer key.
fn row_value(item: &ArtifactItem, key: &str) -> Option<crate::FormLeaf> {
    item.spec_rows.iter().flat_map(|r| std::iter::once(r.leaf.clone()).chain(r.cells.iter()).collect::<Vec<_>>()).find(|l| l.key == key)
}

/// How the toggle labelled `label` is drawn: on or off in every copy the
/// window has (the chat's and its off-screen measure), None when none is.
fn drawn_toggle(label: &str) -> Option<bool> {
    use i_slint_backend_testing::{AccessibleRole, ElementQuery};
    let (window, label) = (crate::window()?, label.to_owned());
    let states: Vec<Option<bool>> = ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_role() == Some(AccessibleRole::Checkbox) && e.accessible_label().is_some_and(|l| l == label.as_str()))
        .find_all()
        .into_iter()
        .map(|e| e.accessible_checked())
        .collect();
    let first = *states.first()?;
    states.iter().all(|s| *s == first).then_some(first).flatten()
}

pub fn render_spec_check(out: String) {
    let (o1, o2, o3, o4) = (out.clone(), out.clone(), out.clone(), out.clone());
    let (o5, o6, o7, o8, o9) = (out.clone(), out.clone(), out.clone(), out.clone(), out);
    let v2_reads = Rc::new(Cell::new(0usize));
    let v2_mark = v2_reads.clone();
    let reads_before = Rc::new(Cell::new(0usize));
    let reads_mark = reads_before.clone();
    let stages: Vec<Stage> = vec![
        ("load", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let switched = control("/__control/render-spec-clients", &json!({"minimums": {"ios": 74, "desktop": 1}}));
            check(switched.is_ok(), &format!("the Host draws render_spec for the desktop: {switched:?}"));
            let loaded = control("/__control/artifact-chat", &json!({"session": "forms", "persona": "Forms", "types": ["render_spec"]}));
            check(loaded.is_ok(), &format!("the Host takes a chat with a form: {loaded:?}"));
            true
        })),
        ("open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("forms").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("forms");
            crate::pump();
            true
        })),
        // ---- drawn, under the reply that made it
        ("drawn", Box::new(move |_, window, elapsed| {
            let Some((row, item)) = card(window) else { return false };
            if item.spec_state != "form" || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(row == format!("made-{ID}"), &format!("the form sits under the reply that made it: {row}"));
            check(item.label == "FORM" && item.title == "Release check", &format!("a form card: {} {}", item.label, item.title));
            let kinds: Vec<String> = item.spec_rows.iter().map(|r| if r.cells.row_count() > 0 { "cells".into() } else { r.leaf.kind.to_string() }).collect();
            check(kinds == ["heading", "text", "card", "choice", "field", "divider", "heading", "field", "toggle", "toggle", "cells", "heading", "text"],
                &format!("every element is drawn in order: {kinds:?}"));
            check(row_value(&item, "testers").is_some_and(|l| l.value == "internal"), "the spec's initial text shows");
            check(row_value(&item, "notify").is_some_and(|l| l.on), "the spec's initial toggle is on");
            check(item.spec_send == "Send my call" && !item.spec_can_send, &format!("Send is the spec's and off: {} {}", item.spec_send, item.spec_can_send));
            check(item.spec_footnote == "2 required answers are missing.", &format!("it says why: {:?}", item.spec_footnote));
            let named = requests("GET", "/artifacts/rs-release").last().map(|r| r["client"].as_str().unwrap_or_default().to_owned()).unwrap_or_default();
            check(named == HEADER, &format!("the desktop names itself to the Host: {named:?}"));
            shot(&o1, "render-spec-01-drawn");
            true
        })),
        // ---- Send waits for the required answers
        ("send too soon", Box::new(|_, window, _| {
            bridge(window).invoke_form_send(ID.into());
            true
        })),
        ("nothing sent", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(posts(SUBMIT).is_empty(), "Send with answers missing posts nothing");
            bridge(window).invoke_form_choose(ID.into(), "decision".into(), "hold".into());
            bridge(window).invoke_form_text(ID.into(), "reason".into(), "Not yet".into());
            true
        })),
        ("too short", Box::new(move |_, window, _| {
            let Some(item) = form(window) else { return false };
            if row_value(&item, "decision").map(|l| l.chosen) != Some(2) {
                return false;
            }
            check(!item.spec_can_send, "a reason under its minimum holds Send");
            check(item.spec_footnote == "Reason needs at least 10 characters.", &format!("and says so: {:?}", item.spec_footnote));
            check(row_value(&item, "reason").is_some_and(|l| l.note == "At least 10 characters."), "under the field too");
            bridge(window).invoke_form_text(ID.into(), "reason".into(), "The crash in Settings is not fixed.".into());
            bridge(window).invoke_form_toggle(ID.into(), "quiet".into(), true);
            true
        })),
        ("ready", Box::new(move |_, window, _| {
            let Some(item) = form(window) else { return false };
            if !item.spec_can_send {
                return false;
            }
            check(item.spec_footnote.is_empty(), &format!("nothing holds Send back: {:?}", item.spec_footnote));
            check(row_value(&item, "quiet").is_some_and(|l| l.on), "the toggle is on");
            check(drawn_toggle("Skip the release notes") == Some(true), &format!("and drawn on: {:?}", drawn_toggle("Skip the release notes")));
            check(drawn_toggle("Tell the testers when it is out") == Some(true), "the spec's own toggle is drawn on");
            shot(&o2, "render-spec-02-ready");
            bridge(window).invoke_form_send(ID.into());
            true
        })),
        // ---- the answer is posted, then the form is read-only
        ("posted", Box::new(move |_, window, _| {
            let posted = posts(SUBMIT);
            let Some(item) = form(window) else { return false };
            if posted.is_empty() || !item.spec_read_only {
                return false;
            }
            check(posted.len() == 1, &format!("one answer is posted: {}", posted.len()));
            let body = &posted[0]["body"];
            check(body["version"] == "preview-1", &format!("for the version read: {}", body["version"]));
            check(body["submission_id"].as_str().is_some_and(|s| s.len() >= 16), "with a submission id the Host takes");
            check(body["answers"] == json!({"intent": "release_call", "state": {"decision": "hold", "reason": "The crash in Settings is not fixed.",
                "testers": "internal", "notify": true, "quiet": true}}), &format!("the answers as the form path takes them: {}", body["answers"]));
            check(posted[0]["client"] == HEADER, &format!("from the desktop: {}", posted[0]["client"]));
            check(item.spec_footnote.starts_with("Answered") && item.spec_footnote.contains("next awake"), &format!("it says how it went: {:?}", item.spec_footnote));
            check(row_value(&item, "reason").is_some_and(|l| l.value == "The crash in Settings is not fixed."), "the answer shows read-only");
            shot(&o3, "render-spec-03-answered");
            true
        })),
        ("still read-only", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            let item = form(window);
            check(item.as_ref().is_some_and(|i| i.spec_read_only && !i.spec_can_send), "it stays read-only once the Host's row moved on");
            // Send again does nothing.
            bridge(window).invoke_form_send(ID.into());
            true
        })),
        // ---- read again from the Host: still answered
        ("reload", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(posts(SUBMIT).len() == 1, "an answered form posts nothing more");
            reads_mark.set(requests("GET", "/artifacts/rs-release").len());
            app.engine.borrow_mut().forget_render_specs();
            app.refresh(&[clarp_engine::Change::Updates]);
            true
        })),
        ("reloaded", Box::new(move |_, window, elapsed| {
            let read_again = requests("GET", "/artifacts/rs-release").len() > reads_before.get();
            let Some(item) = form(window) else { return false };
            if !read_again || !item.spec_read_only || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(row_value(&item, "decision").is_some_and(|l| l.chosen == 2), "the Host's answer shows after a reload");
            check(row_value(&item, "quiet").is_some_and(|l| l.on), "toggles included");
            check(drawn_toggle("Skip the release notes") == Some(true), &format!("and drawn on: {:?}", drawn_toggle("Skip the release notes")));
            check(item.spec_footnote.starts_with("Answered"), &format!("still answered: {:?}", item.spec_footnote));
            shot(&o4, "render-spec-04-reloaded");
            true
        })),
        // ---- catalog version 2: chart, table and buttons
        ("v2 load", Box::new(|_, _, _| {
            let switched = control("/__control/render-spec-clients", &json!({"minimums": {"ios": 74, "desktop": 1}, "v2_minimums": {"desktop": 2}}));
            check(switched.is_ok(), &format!("the Host draws version 2 for the desktop: {switched:?}"));
            let loaded = control("/__control/artifact-chat", &json!({"session": "deploys", "persona": "Deploys", "types": ["render_spec_v2"]}));
            check(loaded.is_ok(), &format!("the Host takes a chat with a v2 form: {loaded:?}"));
            true
        })),
        ("v2 open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("deploys").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("deploys");
            crate::pump();
            true
        })),
        ("v2 drawn", Box::new(move |_, window, elapsed| {
            let Some(item) = v2_form(window) else { return false };
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            let sep = clarp_core::render_spec::device_decimal_separator();
            check(kinds(&item) == ["chart", "chart", "chart", "table", "field", "cells"], &format!("chart, table, field and buttons in order: {:?}", kinds(&item)));
            let leaves: Vec<crate::FormLeaf> = item.spec_rows.iter().map(|r| r.leaf.clone()).collect();
            let (line, bar, pie, table) = (&leaves[0].chart, &leaves[1].chart, &leaves[2].chart, &leaves[3].table);
            check(line.kind == "line" && line.marks.row_count() == 2 && line.keys.row_count() == 2, "the line chart: two series, a legend");
            check(texts(&line.x_ticks) == ["Mon", "Tue", "Wed"], &format!("string x are categories in order: {:?}", texts(&line.x_ticks)));
            let expected: Vec<String> = ["0", "20", "40", "60", "80"].iter().map(|w| format!("{w}{sep}0 %")).collect();
            check(texts(&bar.y_ticks) == expected, &format!("the bar chart's values in decimals 1 with the device separator {sep:?}: {:?}", texts(&bar.y_ticks)));
            check(bar.title == "CPU per host" && bar.y_label == "Load", "the bar chart's title and y label");
            check(pie.kind == "pie" && pie.marks.row_count() == 3, &format!("the pie: three slices ({})", pie.marks.row_count()));
            let keys: Vec<String> = pie.keys.iter().map(|k| k.label.to_string()).collect();
            check(keys == ["eu · 55 %", "us · 35 %", "asia · 10 %"], &format!("the pie's legend names its points: {keys:?}"));
            let cells: Vec<Vec<String>> = table.rows.iter().map(|r| r.cells.iter().map(|c| c.text.to_string()).collect()).collect();
            check(cells == [
                vec!["one".to_owned(), "✓".to_owned(), format!("80{sep}2"), "12500".to_owned(), "eu-north".to_owned()],
                vec!["two".to_owned(), "–".to_owned(), format!("77{sep}1"), "9100".to_owned(), String::new()],
                vec!["three".to_owned(), "✓".to_owned(), String::new(), "4".to_owned(), String::new()],
            ], &format!("the table: checks and dashes, decimals, no grouping, empty cells: {cells:?}"));
            check(buttons(&item) == [("Ship it".to_owned(), "ship".to_owned(), false, false, false), ("Hold".to_owned(), "hold".to_owned(), false, false, false)],
                &format!("two buttons side by side, waiting for the note: {:?}", buttons(&item)));
            check(item.spec_send.is_empty(), &format!("a form with buttons has no footer submit: {:?}", item.spec_send));
            check(item.spec_footnote == "One required answer is missing.", &format!("it says why: {:?}", item.spec_footnote));
            shot(&o5, "render-spec-05-v2-drawn");
            bridge(window).invoke_form_press(V2.into(), "ship".into());
            true
        })),
        ("v2 nothing sent", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(posts(V2_SUBMIT).is_empty(), "a button with the required note missing sends nothing");
            check(window.get_overlay().is_empty(), "and asks nothing");
            bridge(window).invoke_form_text(V2.into(), "note".into(), "Latency is down.".into());
            true
        })),
        ("v2 ready", Box::new(|_, window, _| {
            let Some(item) = v2_form(window) else { return false };
            if !buttons(&item).iter().all(|b| b.2) {
                return false;
            }
            check(item.spec_footnote.is_empty(), &format!("nothing holds the buttons back: {:?}", item.spec_footnote));
            bridge(window).invoke_form_press(V2.into(), "hold".into());
            true
        })),
        ("v2 asked", Box::new(move |_, window, _| {
            if window.get_overlay() != crate::form_view::CONFIRM {
                return false;
            }
            let b = bridge(window);
            check(b.get_confirm_text() == "Hold the release?" && b.get_confirm_destructive(), &format!("Hold asks its question first: {:?} {}", b.get_confirm_text(), b.get_confirm_destructive()));
            check(posts(V2_SUBMIT).is_empty(), "nothing is sent before OK");
            shot(&o6, "render-spec-06-v2-confirm");
            b.invoke_form_confirm_answer(false);
            true
        })),
        ("v2 cancelled", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(posts(V2_SUBMIT).is_empty(), "Cancel sends nothing");
            check(v2_form(window).is_some_and(|i| !i.spec_read_only && buttons(&i).iter().all(|b| b.2)), "and the form stays open");
            bridge(window).invoke_form_press(V2.into(), "ship".into());
            true
        })),
        ("v2 posted", Box::new(move |_, window, _| {
            let posted = posts(V2_SUBMIT);
            let Some(item) = v2_form(window) else { return false };
            if posted.is_empty() || !item.spec_read_only {
                return false;
            }
            check(posted.len() == 1, &format!("one answer is posted: {}", posted.len()));
            check(posted[0]["body"]["answers"] == json!({"intent": "button", "state": {"action": "ship", "note": "Latency is down."}}),
                &format!("intent button with the pressed button's action: {}", posted[0]["body"]["answers"]));
            check(posted[0]["body"]["version"] == "deploy-7", "for the version read");
            check(buttons(&item) == [("Ship it".to_owned(), "ship".to_owned(), false, true, false), ("Hold".to_owned(), "hold".to_owned(), false, false, false)],
                &format!("the chosen button is marked and every button is off: {:?}", buttons(&item)));
            check(item.badge == "Answered · Ship it", &format!("the badge names the button: {:?}", item.badge));
            shot(&o7, "render-spec-07-v2-answered");
            true
        })),
        ("v2 pressed again", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            bridge(window).invoke_form_press(V2.into(), "hold".into());
            bridge(window).invoke_form_press(V2.into(), "ship".into());
            true
        })),
        ("v2 reload", Box::new(move |app, window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(posts(V2_SUBMIT).len() == 1 && window.get_overlay().is_empty(), "an answered form's buttons send and ask nothing");
            v2_mark.set(requests("GET", "/artifacts/rs-deploy").len());
            app.engine.borrow_mut().forget_render_specs();
            app.engine.borrow_mut().load_updates();
            app.refresh(&[clarp_engine::Change::Updates]);
            true
        })),
        ("v2 reloaded", Box::new(move |app, window, elapsed| {
            let read_again = requests("GET", "/artifacts/rs-deploy").len() > v2_reads.get();
            let row = app.engine.borrow().update_artifacts().iter().find(|a| a["artifact_id"] == V2).cloned();
            let Some(item) = v2_form(window) else { return false };
            if !read_again || !item.spec_read_only || elapsed < Duration::from_millis(300) || !row.as_ref().is_some_and(|r| r.get("answered_label").is_some()) {
                return false;
            }
            check(row.is_some_and(|r| r["answered_label"] == "Ship it" && r["answered_action"] == "ship"), "the Host's row names the chosen button");
            check(item.badge == "Answered · Ship it", &format!("still named after a reload: {:?}", item.badge));
            check(buttons(&item).first().is_some_and(|b| b.3), "the Host's answer marks Ship it");
            check(item.spec_footnote.starts_with("Answered"), &format!("answered: {:?}", item.spec_footnote));
            shot(&o8, "render-spec-08-v2-reloaded");
            true
        })),
        // ---- a Host that draws version 1 only for the desktop
        ("v2 off", Box::new(|app, _, _| {
            let off = control("/__control/render-spec-clients", &json!({"minimums": {"ios": 74, "desktop": 1}}));
            check(off.is_ok(), &format!("the Host stops drawing version 2 for the desktop: {off:?}"));
            app.engine.borrow_mut().load_updates();
            true
        })),
        ("v2 plain", Box::new(move |app, window, _| {
            let rows = app.engine.borrow().update_artifacts().to_vec();
            let v2_row = rows.iter().find(|a| a["artifact_id"] == V2);
            if !v2_row.is_some_and(|r| r.get("content_omitted").is_none()) {
                return false;
            }
            let Some((_, item)) = card_of(window, V2) else { return false };
            if !item.spec_state.is_empty() {
                return false;
            }
            check(item.spec_rows.row_count() == 0, "a Host without version 2 for the desktop gives the v2 form as the plain card");
            check(rows.iter().find(|a| a["artifact_id"] == ID).is_some_and(|r| r.get("content_omitted").is_some()), "while the v1 form stays drawn");
            shot(&o9, "render-spec-09-v2-plain");
            app.engine.borrow_mut().select("forms");
            crate::pump();
            true
        })),
        // ---- a Host that does not admit the desktop: the plain row
        ("switch off", Box::new(|app, _, _| {
            let off = control("/__control/render-spec-clients", &json!({"minimums": {"ios": 74}}));
            check(off.is_ok(), &format!("the Host stops drawing render_spec for the desktop: {off:?}"));
            app.engine.borrow_mut().load_updates();
            true
        })),
        ("plain", Box::new(|app, window, _| {
            let row = app.engine.borrow().update_artifacts().iter().find(|a| a["artifact_id"] == ID).cloned();
            if !row.is_some_and(|r| r.get("content_omitted").is_none()) {
                return false;
            }
            let Some((_, item)) = card(window) else { return false };
            check(item.spec_state.is_empty() && item.spec_rows.row_count() == 0, &format!("a Host without the switch gets the plain card: {:?}", item.spec_state));
            true
        })),
    ];
    run_stages(stages);
}
