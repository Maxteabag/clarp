//! Experiment: Clarp's quick switcher (Ctrl+K) as a Rust-native Slint window,
//! to compare with the QML one before deciding anything about the rest of the
//! UI. It reuses the Rust engine: `clarp-core` for the roster, ranking and
//! theme, `clarp-net` for the Host.
//!
//! Live: `clarp-switcher-slint` reads your local Host's roster (read only;
//! Enter prints the chosen session instead of switching the Host's focus).
//! Offline: `--fixture snapshot.json [--query TEXT] [--screenshot out.png
//! --size WxH --scale F]` renders the window with Slint's software renderer
//! into a PNG, with no display.

use std::rc::Rc;

use clarp_core::protocol::display_name;
use clarp_core::roster::{Roster, switcher_rank};
use slint::{ComponentHandle, Model, SharedString, VecModel};

slint::include_modules!();

/// The rows the switcher shows for `needle` (C++/Rust `matchingAgents`):
/// agents whose name, session or directory contain it, best matches first.
fn matching(roster: &Roster, needle: &str) -> Vec<(String, AgentRow)> {
    let needle = needle.trim().to_lowercase();
    let mut rows: Vec<(u8, String, AgentRow)> = roster
        .agents()
        .iter()
        .filter(|agent| !agent.janitor)
        .filter(|agent| {
            needle.is_empty()
                || [display_name(agent), agent.session.as_str(), agent.working_directory.as_str()]
                    .iter()
                    .any(|field| field.to_lowercase().contains(&needle))
        })
        .map(|agent| {
            let name = display_name(agent).to_owned();
            let rank = if needle.is_empty() { 0 } else { switcher_rank(&name, &agent.session, &needle) };
            let state = roster.display_state(&agent.session).unwrap_or_default();
            let detail = [agent.backend.as_str(), state.as_str(), agent.working_directory.as_str()]
                .iter()
                .filter(|part| !part.is_empty())
                .copied()
                .collect::<Vec<_>>()
                .join(" · ");
            let row = AgentRow { name: name.into(), detail: detail.into(), busy: agent.busy, unread: agent.unread };
            (rank, agent.session.clone(), row)
        })
        .collect();
    rows.sort_by_key(|(rank, _, _)| *rank);
    rows.into_iter().map(|(_, session, row)| (session, row)).collect()
}

fn color(value: &str) -> Option<slint::Color> {
    let hex = value.strip_prefix('#')?;
    let n = u32::from_str_radix(hex, 16).ok()?;
    Some(match hex.len() {
        6 => slint::Color::from_rgb_u8((n >> 16) as u8, (n >> 8) as u8, n as u8),
        8 => slint::Color::from_argb_u8((n >> 24) as u8, (n >> 16) as u8, (n >> 8) as u8, n as u8),
        _ => return None,
    })
}

/// The desktop's reading theme, as the QML window uses it.
fn apply_theme(ui: &Switcher, id: &str) {
    let theme = clarp_core::reading_theme::theme(id);
    let pick = |key: &str| theme.get(key).and_then(|v| v.as_str()).and_then(color);
    let palette = ui.global::<Palette>();
    let set = |key: &str, apply: &dyn Fn(slint::Color)| match pick(key) {
        Some(value) => apply(value),
        None => eprintln!("switcher: theme {id} has no colour {key}"),
    };
    set("window", &|c| palette.set_window(c));
    set("raised", &|c| palette.set_raised(c));
    set("control", &|c| palette.set_control(c));
    set("border", &|c| palette.set_border(c));
    set("text", &|c| palette.set_text(c));
    set("mutedText", &|c| palette.set_muted(c));
    set("accent", &|c| palette.set_accent(c));
    set("accentText", &|c| palette.set_accent_text(c));
    set("success", &|c| palette.set_success(c));
}

struct State {
    roster: Roster,
    sessions: Vec<String>,
    model: Rc<VecModel<AgentRow>>,
}

impl State {
    fn refilter(&mut self, needle: &str) {
        let rows = matching(&self.roster, needle);
        self.sessions = rows.iter().map(|(session, _)| session.clone()).collect();
        self.model.set_vec(rows.into_iter().map(|(_, row)| row).collect::<Vec<_>>());
    }
}

thread_local! {
    static HEADLESS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
    // The UI thread's state; Host replies are handed to it on that thread.
    static STATE: std::cell::RefCell<Option<Rc<std::cell::RefCell<State>>>> = const { std::cell::RefCell::new(None) };
}

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let started = std::time::Instant::now();
    let screenshot = arg(&args, "--screenshot");
    HEADLESS.with(|h| h.set(screenshot.is_some()));
    if screenshot.is_some()
        && let Err(error) = headless::install(&args)
    {
        eprintln!("switcher: {error}");
        std::process::exit(1);
    }
    let ui = match Switcher::new() {
        Ok(ui) => ui,
        Err(error) => {
            eprintln!("switcher: cannot open the window: {error}");
            std::process::exit(1);
        }
    };
    apply_theme(&ui, &arg(&args, "--theme").unwrap_or_else(|| "terminal".into()));
    let model = Rc::new(VecModel::<AgentRow>::default());
    ui.set_rows(model.clone().into());
    let state = Rc::new(std::cell::RefCell::new(State { roster: Roster::default(), sessions: Vec::new(), model }));
    STATE.with(|s| *s.borrow_mut() = Some(state.clone()));

    let weak = ui.as_weak();
    let filter_state = state.clone();
    ui.on_query_changed(move |text| {
        filter_state.borrow_mut().refilter(&text);
        if let Some(ui) = weak.upgrade() {
            ui.set_status(format!("{} of {} agents", filter_state.borrow().sessions.len(), filter_state.borrow().roster.agents().len()).into());
        }
    });
    let chosen_state = state.clone();
    ui.on_chosen(move |index| {
        if let Some(session) = chosen_state.borrow().sessions.get(index as usize) {
            println!("{session}");
        }
        // Headless renders have no event loop to quit.
        if let Err(error) = slint::quit_event_loop()
            && !HEADLESS.with(|h| h.get())
        {
            eprintln!("switcher: {error}");
        }
    });
    ui.on_dismissed(|| {
        if let Err(error) = slint::quit_event_loop() {
            eprintln!("switcher: {error}");
        }
    });

    let query = arg(&args, "--query").unwrap_or_default();
    if let Some(path) = arg(&args, "--fixture") {
        let snapshot = std::fs::read_to_string(&path)
            .map_err(|e| e.to_string())
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).map_err(|e| e.to_string()));
        match snapshot {
            Ok(serde_json::Value::Object(object)) => state.borrow_mut().roster.apply_snapshot(&object),
            Ok(_) => eprintln!("switcher: {path} is not a snapshot object"),
            Err(error) => eprintln!("switcher: cannot read {path}: {error}"),
        }
        // In a screenshot the query is typed into the search box instead.
        let initial = if arg(&args, "--screenshot").is_some() { "" } else { query.as_str() };
        state.borrow_mut().refilter(initial);
        let s = state.borrow();
        ui.set_status(format!("{} of {} agents · fixture", s.sessions.len(), s.roster.agents().len()).into());
    } else {
        live::connect(&ui);
    }

    if let Some(path) = screenshot {
        let code = headless::capture(&ui, &query, &path, started);
        std::process::exit(code);
    }
    if let Err(error) = ui.run() {
        eprintln!("switcher: {error}");
        std::process::exit(1);
    }
}

mod live {
    use super::*;
    use clarp_net::{ApiClient, ApiReply};

    /// Reads the roster once from the Host the desktop uses (read only).
    pub fn connect(ui: &Switcher) {
        let base = clarp_core::settings::normalized_base_url(
            &std::env::var("CLARP_BASE_URL").unwrap_or_else(|_| "http://127.0.0.1:7682".into()),
        );
        let token = std::env::var("CLARP_TOKEN").unwrap_or_else(|_| clarp_core::settings::default_token(&base));
        let runtime = match tokio::runtime::Builder::new_multi_thread().worker_threads(1).enable_all().build() {
            Ok(runtime) => Box::leak(Box::new(runtime)),
            Err(error) => {
                ui.set_status(format!("no runtime: {error}").into());
                return;
            }
        };
        let weak = ui.as_weak();
        let client = ApiClient::new(runtime.handle().clone(), move |reply| {
            let weak = weak.clone();
            let queued = slint::invoke_from_event_loop(move || {
                let Some(ui) = weak.upgrade() else { return };
                let Some(state) = STATE.with(|s| s.borrow().clone()) else { return };
                reply_arrived(&ui, &state, reply);
            });
            if let Err(error) = queued {
                eprintln!("switcher: dropped a Host reply: {error}");
            }
        });
        match url::Url::parse(&base) {
            Ok(url) => {
                client.set_endpoint(url, &token);
                client.get("snapshot", "/agents/snapshot", &[]);
                ui.set_status(format!("loading from {base}").into());
            }
            Err(error) => ui.set_status(format!("bad Host URL {base}: {error}").into()),
        }
        Box::leak(Box::new(client));
    }

    fn reply_arrived(ui: &Switcher, state: &Rc<std::cell::RefCell<State>>, reply: ApiReply) {
        match reply {
            ApiReply::Json { object, .. } => {
                let mut state = state.borrow_mut();
                state.roster.apply_snapshot(&object);
                state.refilter(&ui.get_query());
                ui.set_status(format!("{} of {} agents", state.sessions.len(), state.roster.agents().len()).into());
            }
            ApiReply::Failed { message, status, .. } => ui.set_status(format!("Host error {status}: {message}").into()),
            ApiReply::Bytes { .. } => {}
        }
    }
}

mod headless {
    //! Renders the window without a display: Slint's software renderer into
    //! a buffer, saved as PNG. For screenshots and timing in checks.
    use super::*;
    use slint::platform::software_renderer::{MinimalSoftwareWindow, PremultipliedRgbaColor, RepaintBufferType};
    use slint::platform::{Platform, WindowAdapter};

    thread_local! {
        static WINDOW: std::cell::RefCell<Option<Rc<MinimalSoftwareWindow>>> = const { std::cell::RefCell::new(None) };
    }

    struct Offscreen {
        window: Rc<MinimalSoftwareWindow>,
    }

    impl Platform for Offscreen {
        fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, slint::PlatformError> {
            Ok(self.window.clone())
        }
    }

    pub fn install(args: &[String]) -> Result<(), String> {
        let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
        let scale: f32 = arg(args, "--scale").and_then(|s| s.parse().ok()).unwrap_or(1.0);
        window.dispatch_event(slint::platform::WindowEvent::ScaleFactorChanged { scale_factor: scale });
        WINDOW.with(|w| *w.borrow_mut() = Some(window.clone()));
        slint::platform::set_platform(Box::new(Offscreen { window })).map_err(|e| e.to_string())
    }

    pub fn capture(ui: &Switcher, query: &str, path: &str, started: std::time::Instant) -> i32 {
        let Some(window) = WINDOW.with(|w| w.borrow().clone()) else { return 1 };
        let size = arg(&std::env::args().collect::<Vec<_>>(), "--size")
            .and_then(|s| s.split_once('x').and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?))))
            .unwrap_or((640u32, 440u32));
        if let Err(error) = ui.show() {
            eprintln!("switcher: {error}");
            return 1;
        }
        let scale = window.scale_factor();
        window.set_size(slint::PhysicalSize::new((size.0 as f32 * scale) as u32, (size.1 as f32 * scale) as u32));
        // Type the query as keys, the way a person would.
        for character in query.chars() {
            let text = SharedString::from(character.to_string());
            window.dispatch_event(slint::platform::WindowEvent::KeyPressed { text: text.clone() });
            window.dispatch_event(slint::platform::WindowEvent::KeyReleased { text });
        }
        // Then named keys: --press Down,Down,Up,Return.
        for name in arg(&std::env::args().collect::<Vec<_>>(), "--press").unwrap_or_default().split(',').filter(|n| !n.is_empty()) {
            let key = match name {
                "Down" => slint::platform::Key::DownArrow,
                "Up" => slint::platform::Key::UpArrow,
                "Return" => slint::platform::Key::Return,
                "Escape" => slint::platform::Key::Escape,
                other => {
                    eprintln!("switcher: unknown key {other}");
                    return 1;
                }
            };
            let text = SharedString::from(key);
            window.dispatch_event(slint::platform::WindowEvent::KeyPressed { text: text.clone() });
            window.dispatch_event(slint::platform::WindowEvent::KeyReleased { text });
        }
        slint::platform::update_timers_and_animations();
        let (width, height) = ((size.0 as f32 * scale) as usize, (size.1 as f32 * scale) as usize);
        let mut buffer = vec![PremultipliedRgbaColor::default(); width * height];
        let drawn = window.draw_if_needed(|renderer| {
            renderer.render(&mut buffer, width);
        });
        if !drawn {
            eprintln!("switcher: nothing was drawn");
            return 1;
        }
        let first_frame = started.elapsed();
        let mut image = image::RgbaImage::new(width as u32, height as u32);
        for (pixel, color) in image.pixels_mut().zip(buffer.iter()) {
            *pixel = image::Rgba([color.red, color.green, color.blue, 255]);
        }
        if let Err(error) = image.save(path) {
            eprintln!("switcher: cannot save {path}: {error}");
            return 1;
        }
        println!("first frame {} ms, {} rows shown, current row {}", first_frame.as_millis(), ui.get_rows().row_count(), ui.get_current());
        0
    }
}
