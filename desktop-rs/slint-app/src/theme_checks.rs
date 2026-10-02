//! `--check themes --out DIR`: every reading theme (or those named in
//! `CLARP_CHECK_THEMES`, comma-separated) over a long reply with prose, lists,
//! code, a table, a quote and tool calls, with the explorer and an error
//! banner showing, shot as `DIR/theme-ID-end.png` and, scrolled up,
//! `-top.png`, to read by eye. Needs the fake Host.
//!
//! The first theme is applied before the sample loads; for a faithful shot
//! of each theme run it alone (`tools/theme-shots.sh` does). The quote is
//! kept to one line: a quote that wraps is measured a line short and runs
//! into the next row, in every theme.

use std::cell::Cell;
use std::time::{Duration, Instant};

use serde_json::json;
use slint::Model;

use super::{Stage, check, control, run_stages, shot};
use crate::headless;

const REPLY: &str = "## What the reader needs\n\n\
Long replies are read for hours, so the body text gets the most contrast and the calmest colours. \
Secondary text, like **quotes** and tool summaries, steps down one level; timestamps step down again. \
The [APCA guide](https://example.com/apca) asks for Lc 75 at least for body text, and `cargo test -p clarp-core` \
fails when a theme drops below that.\n\n\
Line length matters as much as colour: about sixty to eighty characters per line keeps the eye on the right line \
when it sweeps back, and wide screens tempt us into much longer lines than that.\n\n\
- Body text: Lc 75 and 7:1 on every surface it sits on.\n\
- Muted text: Lc 60 and 4.5:1, including the keyboard cursor.\n\
- Status colours stay apart for protan, deutan and tritan eyes.\n\n\
1. Run the audit.\n2. Read the failures.\n3. Fix the palette, not the rule.\n\n\
```rust\nfn contrast(text: &str, background: &str) -> f64 {\n    let (a, b) = (luminance(text), luminance(background));\n    (a.max(b) + 0.05) / (a.min(b) + 0.05) // WCAG 2\n}\n```\n\n\
| role | needs | state |\n|---|---|---|\n| body | Lc 75 | passes |\n| muted | Lc 60 | passes |\n| faint | Lc 45 | check |\n\n\
> Fix the palette, not the rule.\n\n\
Il1 O0 rn m — the letters that a hyperlegible face keeps apart.";

pub fn themes_check(out: String) {
    let ids: Vec<String> = match std::env::var("CLARP_CHECK_THEMES") {
        Ok(list) if !list.is_empty() => list.split(',').map(str::to_owned).collect(),
        _ => clarp_core::reading_theme::themes().iter().filter_map(|t| t["id"].as_str().map(str::to_owned)).collect(),
    };
    let turns = json!({"session": "rachel", "turns": [
        {"id": "q1", "role": "user", "text": "Which colours keep the transcript readable all day?"},
        {"id": "a1", "role": "assistant", "text": REPLY},
        {"id": "a2", "role": "assistant", "text": "Ran the checks.",
         "tools": [
            {"name": "Bash", "command": "cargo test -p clarp-core", "status": "completed", "result": "test result: ok"},
            {"name": "Bash", "command": "tools/theme-audit.sh paper", "status": "error", "result": "17 failures"},
            {"name": "Read", "file_path": "core/src/reading_themes.json", "status": "running", "result": ""}]},
        {"id": "q2", "role": "user", "text": "Good. Ship the sepia one first."},
        {"id": "a3", "role": "assistant", "text": "Done."},
    ]});
    let first = ids.first().cloned();
    let mut stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            // The first theme is applied before the sample loads, so its
            // rows are laid out in its own font (see below).
            if let Some(first) = first.as_deref() {
                app.engine.borrow_mut().set_reading_theme(first);
            }
            check(control("/__control/turns", &turns).is_ok(), "the Host takes the reading sample");
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("loaded", Box::new(|app, _, _| {
            app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && c.rows().iter().any(|r| r.id.as_str() == "a3"))
        })),
        // The tool calls open, so their status colours show.
        ("tools open", Box::new(|app, window, _| {
            let Some(messages) = app.active_messages() else { return false };
            let Some(row) = messages.iter().find(|r| r.id == "a2") else { return false };
            window.invoke_toggle_activity(app.active_id(), row.id, row.group_id);
            true
        })),
    ];
    // One stage per theme (each must finish within the runner's limit).
    for id in ids {
        let out = out.clone();
        let step = Cell::new(0u8);
        let mark: Cell<Option<Instant>> = Cell::new(None);
        stages.push(("theme", Box::new(move |app, window, _| {
            let since = *mark.get().get_or_insert_with(Instant::now);
            mark.set(Some(since));
            // Fonts load off the UI thread and a wheel animates: let each
            // frame settle before shooting it.
            if since.elapsed() < Duration::from_millis(1200) {
                return false;
            }
            mark.set(None);
            step.set(step.get() + 1);
            match step.get() {
                1 => {
                    app.engine.borrow_mut().set_reading_theme(&id);
                    crate::pump();
                    false
                }
                2 => {
                    window.set_error(format!("Lost the Host at 10:42 — retrying ({id})").into());
                    false
                }
                3 => {
                    shot(&out, &format!("theme-{id}-end"));
                    // Up over the transcript, to the reply's start.
                    headless::wheel(830.0, 400.0, 20000.0);
                    false
                }
                _ => {
                    shot(&out, &format!("theme-{id}-top"));
                    // Back down, so the next theme's first shot is the end.
                    headless::wheel(830.0, 400.0, -20000.0);
                    true
                }
            }
        })));
    }
    run_stages(stages);
}
