//! `--check render-spec --out DIR`: a native form (render_spec, issue 428)
//! published in a chat is drawn in its card under the reply that made it,
//! as iOS draws it. Send waits for the required answers, the answer is
//! posted through the form path, and the form is read-only afterwards and
//! again after the window forgets every form and reads it from the Host.
//! The desktop names itself in `X-Clarp-Client`, and the fake Host draws the
//! kind only for the names its table admits, as the real one does.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, check, control, posts, requests, rows, run_stages, shot};
use crate::{ArtifactBridge, ArtifactItem};

const ID: &str = "rs-release";
const SUBMIT: &str = "/artifacts/rs-release/submit";
const HEADER: &str = "desktop/0.1.0 contract=1";

fn card(window: &crate::AppWindow) -> Option<(String, ArtifactItem)> {
    rows(window).iter().flat_map(|row| row.artifacts.iter().map(|a| (row.id.to_string(), a)).collect::<Vec<_>>()).find(|(_, a)| a.id == ID)
}

fn form(window: &crate::AppWindow) -> Option<ArtifactItem> {
    card(window).map(|(_, a)| a).filter(|a| a.spec_state == "form")
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
    let (o1, o2, o3, o4) = (out.clone(), out.clone(), out.clone(), out);
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
