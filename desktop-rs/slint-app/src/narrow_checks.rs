//! `--check narrow --out DIR`: the panes stay inside a narrow window.
//!
//! The window is shrunk to 520 logical px and the chat and composer frames
//! must keep their right edge inside it: at a small width the content wraps
//! or clips inside the pane instead of running off the window edge (the
//! owner's report, 2026-10-10: the composer ran off the right edge). Any
//! element whose right edge is past the window is printed, for the log.

use std::cell::Cell;

use i_slint_core::accessibility::AccessibleStringProperty;
use i_slint_core::item_tree::ItemRc;
use i_slint_core::window::WindowInner;
use slint::{ComponentHandle, LogicalSize};

use super::{Stage, app_now, check, run_stages, shot};

const NARROW: f32 = 520.0;
const NARROW_H: f32 = 720.0;

fn id_of(item: &ItemRc) -> Option<String> {
    item.accessible_string_property(AccessibleStringProperty::Id).map(|id| id.to_string())
}

/// (x, y, width, height) in window coordinates.
fn rect(item: &ItemRc) -> (f32, f32, f32, f32) {
    let geometry = item.geometry();
    let origin = item.map_to_window(geometry.origin);
    (origin.x, origin.y, geometry.size.width, geometry.size.height)
}

/// Every element in the tree, with its geometry (id is "" when it has none).
fn collect(item: &ItemRc, out: &mut Vec<(String, f32, f32, f32, f32)>) {
    let (x, y, w, h) = rect(item);
    out.push((id_of(item).unwrap_or_default(), x, y, w, h));
    let mut child = item.first_child();
    while let Some(current) = child {
        collect(&current, out);
        child = current.next_sibling();
    }
}

pub(super) fn narrow_check(out: String) {
    let out2 = out.clone();
    let prepared = Cell::new(false);
    let stages: Vec<Stage> = vec![
        ("open", Box::new(move |app, w, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            if app.engine.borrow().conversation("rachel").map_or(true, |c| c.loading() || c.rows().is_empty()) {
                return false;
            }
            if !prepared.get() {
                app.engine.borrow_mut().select("rachel");
                app_now().focus_composer();
                w.window().set_size(LogicalSize::new(NARROW, NARROW_H));
                prepared.set(true);
            }
            true
        })),
        ("narrow settled", Box::new(|_, w, _| {
            // Keep the requested size in case a resize round-trip dropped it.
            if (w.window().size().width as f32 / w.window().scale_factor() - NARROW).abs() > 1.0 {
                w.window().set_size(LogicalSize::new(NARROW, NARROW_H));
                return false;
            }
            true
        })),
        ("frames inside", Box::new(move |_, w, _| {
            let inner = WindowInner::from_pub(w.window());
            let root = ItemRc::new_root(inner.component());
            let window_width = rect(&root).2;
            let id = app_now().active_id();
            let mut rows = Vec::new();
            collect(&root, &mut rows);
            for wanted in [format!("chat:{id}"), format!("composer-box:{id}")] {
                match rows.iter().find(|r| r.0 == wanted) {
                    Some((_, x, _, width, _)) => {
                        let right = x + width;
                        if right > window_width + 0.5 {
                            let mut over: Vec<&(String, f32, f32, f32, f32)> = rows.iter().filter(|r| r.1 + r.3 > window_width + 0.5).collect();
                            over.sort_by(|a, b| (a.1 + a.3).total_cmp(&(b.1 + b.3)));
                            for r in over.iter().rev().take(30) {
                                let name = if r.0.is_empty() { "<anon>".to_owned() } else { r.0.clone() };
                                eprintln!("narrow: {name} x={:.0} y={:.0} w={:.0} h={:.0} right={:.0}", r.1, r.2, r.3, r.4, r.1 + r.3);
                            }
                            check(false, &format!("{wanted} ends at {right:.0}px, past the {window_width:.0}px window"));
                            return true;
                        }
                    }
                    None => {
                        check(false, &format!("{wanted} is not found"));
                        return true;
                    }
                }
            }
            check(true, &format!("the chat and composer stay inside a {window_width:.0}px window"));
            shot(&out2, "narrow-01-520");
            true
        })),
    ];
    run_stages(stages);
}
