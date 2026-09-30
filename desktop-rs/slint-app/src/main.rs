//! The Clarp desktop in Slint: a thin view over `clarp-engine`. The engine
//! owns behaviour; this file turns its state into Slint models and the
//! window's actions into engine commands.
//!
//! `clarp-slint` opens the window. `--headless` runs it without a display on
//! Slint's software renderer (for checks), and `--e2e-out DIR` drives it like
//! a person would, saving a screenshot per stage (see `driver.rs`).

mod driver;
mod headless;

use std::cell::RefCell;
use std::rc::Rc;

use clarp_core::protocol::display_name;
use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

slint::include_modules!();

thread_local! {
    static APP: RefCell<Option<Rc<App>>> = const { RefCell::new(None) };
}

pub struct App {
    pub engine: RefCell<Engine>,
    pub window: slint::Weak<AppWindow>,
    chats: Rc<VecModel<ChatRow>>,
    messages: Rc<VecModel<MessageRow>>,
}

pub fn app() -> Option<Rc<App>> {
    APP.with(|a| a.borrow().clone())
}

/// The Slint app's own settings, apart from the Qt apps'.
fn settings() -> Settings {
    match std::env::var("CLARP_SETTINGS") {
        Ok(value) if value == "off" => Settings::in_memory(),
        Ok(value) if !value.is_empty() => Settings::at(value),
        _ => match clarp_core::settings::config_home() {
            Some(config) => Settings::at(config.join("MaxTeaBag").join("ClarpSlint").join("settings.json")),
            None => Settings::in_memory(),
        },
    }
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

pub fn apply_theme(window: &AppWindow, id: &str) {
    let theme = clarp_core::reading_theme::theme(id);
    let pick = |key: &str| theme.get(key).and_then(|v| v.as_str()).and_then(color);
    let palette = window.global::<Palette>();
    let pairs: [(&str, &dyn Fn(slint::Color)); 20] = [
        ("window", &|c| palette.set_window(c)),
        ("raised", &|c| palette.set_raised(c)),
        ("sunken", &|c| palette.set_sunken(c)),
        ("control", &|c| palette.set_control(c)),
        ("hover", &|c| palette.set_hover(c)),
        ("border", &|c| palette.set_border(c)),
        ("rule", &|c| palette.set_rule(c)),
        ("text", &|c| palette.set_text(c)),
        ("chromeText", &|c| palette.set_chrome_text(c)),
        ("mutedText", &|c| palette.set_muted(c)),
        ("faintText", &|c| palette.set_faint(c)),
        ("accent", &|c| palette.set_accent(c)),
        ("accentText", &|c| palette.set_accent_text(c)),
        ("bubble", &|c| palette.set_bubble(c)),
        ("success", &|c| palette.set_success(c)),
        ("warning", &|c| palette.set_warning(c)),
        ("danger", &|c| palette.set_danger(c)),
        ("dangerSurface", &|c| palette.set_danger_surface(c)),
        ("link", &|c| palette.set_link(c)),
        ("selection", &|_| {}),
    ];
    for (key, apply) in pairs {
        match pick(key) {
            Some(value) => apply(value),
            None if key != "selection" => eprintln!("clarp-slint: theme {id} has no colour {key}"),
            None => {}
        }
    }
    if let Some(size) = theme.get("fontPixelSize").and_then(|v| v.as_f64()) {
        palette.set_body_size(size as f32);
    }
}

impl App {
    fn refresh(&self, changes: &[Change]) {
        let Some(window) = self.window.upgrade() else { return };
        let engine = self.engine.borrow();
        let selected = engine.selected_session().to_owned();
        let roster_changed = changes.iter().any(|c| matches!(c, Change::Roster | Change::Selection));
        if roster_changed {
            let rows: Vec<ChatRow> = engine
                .roster()
                .agents()
                .iter()
                .filter(|a| !a.janitor)
                .map(|agent| {
                    let name = display_name(agent).to_owned();
                    let state = engine.roster().display_state(&agent.session).unwrap_or_default();
                    ChatRow {
                        session: agent.session.clone().into(),
                        initial: name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default().into(),
                        name: name.into(),
                        detail: if agent.busy { format!("{} · working…", agent.backend) } else { format!("{} · {state}", agent.backend) }.into(),
                        busy: agent.busy,
                        unread: agent.unread,
                        selected: agent.session == selected,
                    }
                })
                .collect();
            self.chats.set_vec(rows);
        }
        if changes.iter().any(|c| matches!(c, Change::Selection) || matches!(c, Change::Conversation(s) if *s == selected)) {
            let rows: Vec<MessageRow> = engine
                .conversation(&selected)
                .map(|conversation| {
                    conversation
                        .rows()
                        .iter()
                        .map(|m| {
                            let author = if m.activity { "activity" } else if m.role == "user" { "user" } else { "assistant" };
                            let text = if m.display_text.is_empty() { m.text.clone() } else { m.display_text.clone() };
                            let meta = if m.tools.is_empty() { String::new() } else { format!("{} tool call{}", m.tools.len(), if m.tools.len() == 1 { "" } else { "s" }) };
                            MessageRow { id: m.id.clone().into(), author: author.into(), text: text.into(), meta: meta.into(), pending: m.pending, failed: m.delivery_failed }
                        })
                        .collect()
                })
                .unwrap_or_default();
            self.messages.set_vec(rows);
        }
        let agent = engine.selected_agent();
        window.set_selected_name(agent.map(|a| display_name(a).to_owned()).unwrap_or_default().into());
        window.set_selected_detail(
            agent.map(|a| format!("{} · {}", a.backend, a.working_directory)).unwrap_or_default().into(),
        );
        window.set_busy(agent.is_some_and(|a| a.busy));
        window.set_connection(engine.connection_state().into());
        let conversation_error = engine.conversation(&selected).map(|c| {
            if c.error().is_empty() { c.voice_error().to_owned() } else { c.error().to_owned() }
        });
        let error = if engine.error().is_empty() { conversation_error.unwrap_or_default() } else { engine.error().to_owned() };
        window.set_error(SharedString::from(error));
        window.set_sending(engine.sending());
    }
}

/// Applies what the engine has queued; scheduled on the UI thread by wake.
pub fn pump() {
    let Some(app) = app() else { return };
    let changes = app.engine.borrow_mut().pump();
    if !changes.is_empty() {
        app.refresh(&changes);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let e2e_out = args.iter().position(|a| a == "--e2e-out").and_then(|i| args.get(i + 1)).cloned();
    let headless = e2e_out.is_some() || args.iter().any(|a| a == "--headless");
    if headless && let Err(error) = headless::install(1280, 800, 1.0) {
        eprintln!("clarp-slint: {error}");
        std::process::exit(1);
    }
    let window = match AppWindow::new() {
        Ok(window) => window,
        Err(error) => {
            eprintln!("clarp-slint: cannot open the window: {error}");
            std::process::exit(1);
        }
    };
    let settings = settings();
    apply_theme(&window, &settings.string("appearance/readingTheme", clarp_core::reading_theme::default_theme_id()));
    let engine = match Engine::new(Config::from_env(settings), || {
        if let Err(error) = slint::invoke_from_event_loop(pump) {
            eprintln!("clarp-slint: dropped an engine wake: {error}");
        }
    }) {
        Ok(engine) => engine,
        Err(error) => {
            eprintln!("clarp-slint: {error}");
            std::process::exit(1);
        }
    };
    let chats = Rc::new(VecModel::<ChatRow>::default());
    let messages = Rc::new(VecModel::<MessageRow>::default());
    window.set_chats(ModelRc::from(chats.clone()));
    window.set_messages(ModelRc::from(messages.clone()));
    let state = Rc::new(App { engine: RefCell::new(engine), window: window.as_weak(), chats, messages });
    APP.with(|a| *a.borrow_mut() = Some(state.clone()));

    window.on_chat_chosen(|session| {
        if let Some(app) = app() {
            app.engine.borrow_mut().select(&session);
            pump_now(&app);
        }
    });
    window.on_send(|text| {
        if let Some(app) = app() {
            app.engine.borrow_mut().send(&text);
            pump_now(&app);
        }
    });
    window.on_stop(|| {
        if let Some(app) = app() {
            app.engine.borrow_mut().stop();
        }
    });
    window.on_dismiss_error(|| {
        if let Some(app) = app() {
            app.engine.borrow_mut().clear_error();
            pump_now(&app);
        }
    });

    state.engine.borrow_mut().start();
    drop(state);
    if let Some(out) = e2e_out {
        driver::start(out);
    }
    if let Err(error) = window.run() {
        eprintln!("clarp-slint: {error}");
        std::process::exit(1);
    }
    std::process::exit(driver::exit_code());
}

/// Commands change state synchronously (an optimistic row, a selection):
/// show it without waiting for the next wake.
fn pump_now(app: &Rc<App>) {
    let changes = app.engine.borrow_mut().pump();
    let mut all = changes;
    all.push(Change::Selection);
    app.refresh(&all);
}
