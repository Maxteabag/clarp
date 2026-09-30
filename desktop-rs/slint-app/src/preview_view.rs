//! Worktree preview versions (the Qt app's `PreviewVersions` over
//! `clarp_core::preview`): only in the preview install (or the
//! `--preview-versions` manager), a python helper lists the built versions
//! every 15 s and pins one; the window then reopens itself on the same Host
//! and conversation. `CLARP_TEST_PREVIEW_FIXTURE=1` shows a fixed catalog
//! and never runs the helper, for checks.

use std::cell::RefCell;
use std::process::{Command, Stdio};
use std::rc::Rc;
use std::time::Duration;

use clarp_core::preview::{Finished, Launch, PREVIEW_PYTHON, PreviewVersions, REFRESH_MS, relaunch_environment};
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{App, AppWindow, PreviewVersion, PreviewWindow};

struct State {
    versions: PreviewVersions,
    busy: bool,
    timer: Option<slint::Timer>,
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
    static MANAGER: RefCell<Option<slint::Weak<PreviewWindow>>> = const { RefCell::new(None) };
}

fn launch(manager: bool) -> Launch {
    let home = std::env::var("HOME").unwrap_or_default();
    let helper = clarp_core::preview::helper_path(&home);
    let fixture = std::env::var_os("CLARP_TEST_PREVIEW_FIXTURE").is_some();
    Launch {
        helper_exists: std::path::Path::new(&helper).exists(),
        home,
        instance_name: std::env::var("CLARP_INSTANCE_NAME").unwrap_or_default(),
        manager,
        screenshot: fixture,
        screenshot_scenario: if fixture { "preview-versions".into() } else { String::new() },
        executable_hash: clarp_core::preview::file_sha256(std::path::Path::new("/proc/self/exe")).unwrap_or_default(),
    }
}

/// Starts following the preview catalog (nothing outside a preview install).
pub fn start(manager: bool) {
    let versions = PreviewVersions::new(&launch(manager));
    let polls = versions.polls();
    STATE.with(|slot| *slot.borrow_mut() = Some(State { versions, busy: false, timer: None }));
    if polls {
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::Repeated, Duration::from_millis(REFRESH_MS), refresh);
        STATE.with(|slot| {
            if let Some(state) = slot.borrow_mut().as_mut() {
                state.timer = Some(timer);
            }
        });
        refresh();
    }
}

pub fn enabled() -> bool {
    STATE.with(|slot| slot.borrow().as_ref().is_some_and(|s| s.versions.enabled()))
}

/// The label of the newer installed build, when one waits.
fn update_label(versions: &PreviewVersions) -> String {
    let catalog = versions.catalog();
    let current = catalog.get("current").and_then(Value::as_str).unwrap_or_default();
    if current.is_empty() || current == versions.running_hash() {
        return String::new();
    }
    catalog
        .get("versions")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .find(|v| v.get("hash").and_then(Value::as_str) == Some(current))
        .and_then(|v| v.get("label").and_then(Value::as_str))
        .unwrap_or("new build")
        .to_owned()
}

fn rows(versions: &PreviewVersions) -> Vec<PreviewVersion> {
    let catalog = versions.catalog();
    let text = |v: &Value, key: &str| v.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    let current = text(catalog, "current");
    let latest = text(catalog, "latest");
    catalog
        .get("versions")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|v| {
            let hash = text(v, "hash");
            let mut label = text(v, "label");
            if hash == versions.running_hash() {
                label += "   · running";
            }
            if hash == current {
                label += "   · installed";
            }
            if hash == latest {
                label += "   · latest";
            }
            PreviewVersion { hash: hash.into(), label: label.into() }
        })
        .collect()
}

/// Shows the catalog in whichever window there is.
pub fn show() {
    STATE.with(|slot| {
        let slot = slot.borrow();
        let Some(state) = slot.as_ref() else { return };
        let versions = &state.versions;
        let pinned = versions.catalog().get("pinned").and_then(Value::as_str).is_some_and(|p| !p.is_empty());
        if let Some(window) = crate::window() {
            window.set_preview_enabled(versions.enabled());
            window.set_preview_versions(ModelRc::new(VecModel::from(rows(versions))));
            window.set_preview_busy(state.busy);
            window.set_preview_pinned(pinned);
            window.set_preview_error(versions.error().into());
            window.set_preview_notice(versions.notice().into());
            window.set_preview_update_label(update_label(versions).into());
            window.set_preview_can_restart(versions.restart_allowed);
        }
        if let Some(manager) = MANAGER.with(|m| m.borrow().as_ref().and_then(slint::Weak::upgrade)) {
            manager.set_versions(ModelRc::new(VecModel::from(rows(versions))));
            manager.set_busy(state.busy);
            manager.set_pinned(pinned);
            manager.set_error(versions.error().into());
            manager.set_notice(versions.notice().into());
        }
    });
}

/// What restarting needs to know: may the window restart now (nothing is
/// sending, uploading, recording, transcribing or playing), and which chat
/// it should reopen.
pub fn follow(app: &App) {
    let engine = app.engine.borrow();
    let voice_busy = crate::platform::audio::with(|audio| {
        audio.recording() || audio.playing() || audio.transcriptions_in_flight() > 0
    })
    .unwrap_or(false);
    let restart = !engine.sending() && !engine.uploading() && !voice_busy;
    let (session, host) = (engine.selected_session().to_owned(), engine.base_url().to_owned());
    drop(engine);
    STATE.with(|slot| {
        if let Some(state) = slot.borrow_mut().as_mut() {
            state.versions.restart_allowed = restart;
            state.versions.selected_session = session;
            state.versions.selected_host = host;
        }
    });
}

fn refresh() {
    let arguments = STATE.with(|slot| slot.borrow().as_ref().and_then(|s| s.versions.refresh(s.busy)));
    if let Some(arguments) = arguments {
        run_helper(arguments);
    }
}

/// Pins `hash` ("latest" resumes updates).
pub fn select(hash: &str) {
    if let Some(app) = crate::app() {
        follow(&app);
    }
    let arguments = STATE.with(|slot| {
        let mut slot = slot.borrow_mut();
        let state = slot.as_mut()?;
        let busy = state.busy;
        state.versions.select_version(hash, busy)
    });
    match arguments {
        Some(arguments) => run_helper(arguments),
        None => show(),
    }
}

fn run_helper(arguments: Vec<String>) {
    STATE.with(|slot| {
        if let Some(state) = slot.borrow_mut().as_mut() {
            state.busy = true;
        }
    });
    show();
    crate::platform::runtime::handle().spawn_blocking(move || {
        let output = Command::new(PREVIEW_PYTHON).args(&arguments).stdin(Stdio::null()).stderr(Stdio::inherit()).output();
        if let Err(error) = slint::invoke_from_event_loop(move || finished(output)) {
            eprintln!("clarp-slint: dropped a preview helper result: {error}");
        }
    });
}

fn finished(output: std::io::Result<std::process::Output>) {
    let next = STATE.with(|slot| {
        let mut slot = slot.borrow_mut();
        let state = slot.as_mut()?;
        state.busy = false;
        Some(match output {
            Err(error) => {
                eprintln!("clarp-slint: cannot run {PREVIEW_PYTHON}: {error}");
                state.versions.start_failed();
                Finished::Nothing
            }
            Ok(output) => state.versions.finished(output.status.success(), &output.stdout),
        })
    });
    match next {
        Some(Finished::Refresh) => {
            show();
            refresh();
        }
        Some(Finished::Relaunch) => relaunch(),
        _ => show(),
    }
}

/// Reopens on the pinned build: the helper starts it once this process has
/// gone, on the same Host and conversation.
fn relaunch() {
    let context = STATE.with(|slot| slot.borrow().as_ref().map(|s| (s.versions.restart_context(std::process::id()), s.versions.helper().to_owned())));
    let Some((context, helper)) = context else { return };
    let host = context["host"].as_str().unwrap_or_default().to_owned();
    let session = context["session"].as_str().unwrap_or_default().to_owned();
    let arguments = clarp_core::preview::relaunch_arguments(&helper, std::process::id());
    let spawned = Command::new(PREVIEW_PYTHON).args(&arguments).envs(relaunch_environment(&host, &session)).stdin(Stdio::null()).spawn();
    match spawned {
        Ok(_) => {
            if let Err(error) = slint::quit_event_loop() {
                eprintln!("clarp-slint: {error}");
            }
        }
        Err(error) => {
            eprintln!("clarp-slint: relaunch failed: {error}");
            STATE.with(|slot| {
                if let Some(state) = slot.borrow_mut().as_mut() {
                    state.versions.relaunch_failed();
                }
            });
            show();
        }
    }
}

/// `--preview-versions`: the manager window alone.
pub fn run_manager() -> ! {
    let window = match PreviewWindow::new() {
        Ok(window) => window,
        Err(error) => {
            eprintln!("clarp-slint: cannot open the preview versions window: {error}");
            std::process::exit(1);
        }
    };
    MANAGER.with(|m| *m.borrow_mut() = Some(window.as_weak()));
    start(true);
    window.on_choose(|hash| select(&hash));
    window.on_done(|| {
        if let Err(error) = slint::quit_event_loop() {
            eprintln!("clarp-slint: {error}");
        }
    });
    show();
    if let Err(error) = window.run() {
        eprintln!("clarp-slint: {error}");
        std::process::exit(1);
    }
    std::process::exit(0);
}

/// Wires the main window's panel and banner.
pub fn wire(window: &AppWindow) {
    window.on_preview_choose(|hash| select(&hash));
    window.on_preview_update(|| {
        let current = STATE.with(|slot| {
            slot.borrow().as_ref().and_then(|s| s.versions.catalog().get("current").and_then(Value::as_str).map(str::to_owned))
        });
        if let Some(current) = current {
            select(&current);
        }
    });
}

/// `update-preview` (Ctrl+Alt+U): the newer build when one waits, else the
/// panel.
pub fn update_or_open(app: &Rc<App>, window: &AppWindow) -> bool {
    if !enabled() {
        return false;
    }
    follow(app);
    let label = STATE.with(|slot| slot.borrow().as_ref().map(|s| update_label(&s.versions)).unwrap_or_default());
    if !label.is_empty() && window.get_preview_can_restart() && !window.get_preview_busy() {
        window.invoke_preview_update();
    } else {
        open(app, window);
    }
    true
}

pub fn open(app: &Rc<App>, window: &AppWindow) {
    follow(app);
    show();
    crate::commands::open_overlay(app, window, "preview");
    window.invoke_open_preview();
    refresh();
}
