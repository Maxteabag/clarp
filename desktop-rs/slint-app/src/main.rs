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
    rooms: Rc<VecModel<ChatRow>>,
    archived: Rc<VecModel<ChatRow>>,
    messages: Rc<VecModel<MessageRow>>,
    sidebar: RefCell<clarp_core::sidebar::Sidebar>,
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
    // The standard widgets follow the theme's light or dark scheme.
    let light = theme.get("light").and_then(|v| v.as_bool()).unwrap_or(false);
    window.global::<WidgetPalette>().set_color_scheme(if light { slint::language::ColorScheme::Light } else { slint::language::ColorScheme::Dark });
    if let Some(size) = theme.get("fontPixelSize").and_then(|v| v.as_f64()) {
        palette.set_body_size(size as f32);
    }
}

fn stamp(epoch_millis: i64) -> String {
    if epoch_millis <= 0 {
        return String::new();
    }
    clarp_core::time_format::chat_stamp(epoch_millis, &chrono::Local::now())
}

fn initial(name: &str) -> SharedString {
    name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default().into()
}

fn chat_row(row: &clarp_core::roster::AgentRow, depth: usize, selected: &str) -> ChatRow {
    let preview = if !row.last_message.is_empty() { row.last_message.clone() } else { row.working_directory.clone() };
    let activity = if row.busy && !row.status_text.is_empty() { row.status_text.clone() } else { String::new() };
    ChatRow {
        session: row.session.clone().into(),
        initial: initial(&row.name),
        name: row.name.clone().into(),
        stamp: stamp(row.last_activity).into(),
        preview: preview.into(),
        activity: activity.into(),
        depth: depth as i32,
        queued: row.queue_count,
        busy: row.busy,
        unread: row.unread,
        muted: row.muted,
        selected: row.session == selected,
    }
}

/// Inline Markdown as Slint styled text; plain text if Slint cannot parse it.
fn styled(markdown: &str, literal: bool) -> slint::StyledText {
    if literal {
        return slint::StyledText::from_plain_text(markdown);
    }
    slint::StyledText::from_markdown(markdown).unwrap_or_else(|_| slint::StyledText::from_plain_text(markdown))
}

fn message_block(block: &clarp_engine::blocks::Block, literal: bool) -> MessageBlock {
    use clarp_engine::blocks::Block;
    let empty = || ModelRc::new(VecModel::<TableRow>::default());
    match block {
        Block::Prose(markdown) => MessageBlock { kind: "prose".into(), styled: styled(markdown, literal), text: SharedString::new(), level: 0, rows: empty() },
        Block::Heading { level, markdown } => MessageBlock {
            kind: "heading".into(),
            styled: styled(&format!("**{markdown}**"), false),
            text: SharedString::new(),
            level: i32::from(*level),
            rows: empty(),
        },
        Block::Code { text, .. } => MessageBlock { kind: "code".into(), styled: slint::StyledText::default(), text: text.clone().into(), level: 0, rows: empty() },
        Block::Quote(markdown) => MessageBlock { kind: "quote".into(), styled: styled(markdown, false), text: SharedString::new(), level: 0, rows: empty() },
        Block::Table(rows) => MessageBlock {
            kind: "table".into(),
            styled: slint::StyledText::default(),
            text: SharedString::new(),
            level: 0,
            rows: ModelRc::new(VecModel::from(
                rows.iter()
                    .enumerate()
                    .map(|(index, cells)| TableRow {
                        cells: ModelRc::new(VecModel::from(cells.iter().map(|c| styled(c, false)).collect::<Vec<_>>())),
                        header: index == 0,
                    })
                    .collect::<Vec<_>>(),
            )),
        },
        Block::Rule => MessageBlock { kind: "rule".into(), styled: slint::StyledText::default(), text: SharedString::new(), level: 0, rows: empty() },
    }
}

/// Only web and mail links open, in the desktop's browser; tests record
/// them to `CLARP_TEST_OPEN_URL` instead.
fn open_link(url: &str) {
    let openable = url.starts_with("https://") || url.starts_with("http://") || url.starts_with("mailto:");
    if !openable {
        eprintln!("clarp-slint: not opening {url}: only web and mail links open");
        return;
    }
    if let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL") {
        use std::io::Write;
        let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{url}"));
        if let Err(error) = written {
            eprintln!("clarp-slint: could not record {url}: {error}");
        }
        return;
    }
    if let Err(error) = std::process::Command::new("xdg-open").arg(url).spawn() {
        eprintln!("clarp-slint: could not open {url}: {error}");
    }
}

impl App {
    /// The chat list, through the same search/scope/nesting rules as the
    /// Qt sidebar (`clarp_core::sidebar`).
    fn chat_rows(&self, engine: &Engine, window: &AppWindow) -> Vec<ChatRow> {
        use clarp_core::sidebar::{FilterInput, TreeInput};
        let rows = engine.roster().rows();
        let trees: Vec<TreeInput> = rows
            .iter()
            .map(|r| TreeInput {
                session: r.session.clone(),
                agent_id: r.agent_id.clone(),
                agent_role: r.agent_role.clone(),
                parent_agent_id: r.parent_agent_id.clone(),
                helper_state: r.helper_state.clone(),
            })
            .collect();
        let filters: Vec<FilterInput> = rows
            .iter()
            .map(|r| FilterInput {
                session: r.session.clone(),
                unread: r.unread,
                name: r.name.clone(),
                backend: r.backend.clone(),
                last_message: r.last_message.clone(),
                working_directory: r.working_directory.clone(),
            })
            .collect();
        let mut sidebar = self.sidebar.borrow_mut();
        sidebar.query = window.get_query().to_string();
        sidebar.unread_only = window.get_scope() == "unread";
        sidebar.rebuild(&trees);
        let selected = engine.selected_session();
        sidebar
            .visible(&trees, &filters)
            .into_iter()
            .filter_map(|session| rows.iter().find(|r| r.session == session))
            .map(|row| chat_row(row, sidebar.depth(&row.session), selected))
            .collect()
    }

    fn refresh(&self, changes: &[Change]) {
        let Some(window) = self.window.upgrade() else { return };
        let engine = self.engine.borrow();
        if changes.contains(&Change::Preferences) {
            apply_theme(&window, &engine.reading_theme());
        }
        let selected = engine.selected_session().to_owned();
        let list_changed = changes.iter().any(|c| matches!(c, Change::Roster | Change::Selection | Change::Rooms | Change::Archive));
        if list_changed {
            self.chats.set_vec(self.chat_rows(&engine, &window));
            let rooms: Vec<ChatRow> = engine
                .rooms()
                .iter()
                .filter_map(|room| room.as_object())
                .map(|room| {
                    let session = clarp_core::json::string(room, "conversation_id");
                    let title = engine.chat_name(&session);
                    ChatRow {
                        initial: "↔".into(),
                        name: title.into(),
                        stamp: String::new().into(),
                        preview: clarp_core::json::string(room, "preview").into(),
                        unread: room.get("unread").and_then(|v| v.as_bool()).unwrap_or(false),
                        selected: session == selected,
                        session: session.into(),
                        ..ChatRow::default()
                    }
                })
                .collect();
            self.rooms.set_vec(rooms);
            let archived: Vec<ChatRow> = engine.archived().rows().iter().map(|row| chat_row(row, 0, &selected)).collect();
            self.archived.set_vec(archived);
            window.set_unread_rooms(engine.unread_rooms() as i32);
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
                            // The user's own words stay literal; replies are Markdown.
                            let blocks = if author == "user" { vec![clarp_engine::blocks::Block::Prose(text.clone())] } else { clarp_engine::blocks::blocks(&text) };
                            MessageRow {
                                id: m.id.clone().into(),
                                author: author.into(),
                                blocks: ModelRc::new(VecModel::from(blocks.iter().map(|b| message_block(b, author == "user")).collect::<Vec<_>>())),
                                meta: meta.into(),
                                pending: m.pending,
                                failed: m.delivery_failed,
                            }
                        })
                        .collect()
                })
                .unwrap_or_default();
            self.messages.set_vec(rows);
        }
        let agent = engine.selected_agent();
        window.set_selected_name(if selected.is_empty() { String::new() } else { engine.chat_name(&selected) }.into());
        window.set_selected_detail(agent.map(|a| format!("{} · {}", a.backend, a.working_directory)).unwrap_or_default().into());
        window.set_selected_model(agent.map(|a| a.model.clone()).unwrap_or_default().into());
        window.set_selected_effort(agent.map(|a| a.effort.clone()).unwrap_or_default().into());
        window.set_busy(agent.is_some_and(|a| a.busy));
        window.set_connection(engine.connection_state().into());
        window.set_muted(engine.muted());
        let name = engine.server_name();
        window.set_server_initial(initial(if name.is_empty() { "C" } else { name }));
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
    let arg = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let shot = arg("--shot");
    let headless = e2e_out.is_some() || shot.is_some() || args.iter().any(|a| a == "--headless");
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
    let theme = std::env::args()
        .skip_while(|a| a != "--theme")
        .nth(1)
        .unwrap_or_else(|| settings.string("appearance/readingTheme", clarp_core::reading_theme::default_theme_id()));
    apply_theme(&window, &theme);
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
    let rooms = Rc::new(VecModel::<ChatRow>::default());
    let archived = Rc::new(VecModel::<ChatRow>::default());
    let messages = Rc::new(VecModel::<MessageRow>::default());
    window.set_chats(ModelRc::from(chats.clone()));
    window.set_rooms(ModelRc::from(rooms.clone()));
    window.set_archived(ModelRc::from(archived.clone()));
    window.set_messages(ModelRc::from(messages.clone()));
    let state = Rc::new(App {
        engine: RefCell::new(engine),
        window: window.as_weak(),
        chats,
        rooms,
        archived,
        messages,
        sidebar: RefCell::new(clarp_core::sidebar::Sidebar::default()),
    });
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
    window.on_filter_changed(|| {
        if let Some(app) = app() {
            app.refresh(&[Change::Roster]);
        }
    });
    window.on_toggle_muted(|| {
        if let Some(app) = app() {
            let muted = app.engine.borrow().muted();
            app.engine.borrow_mut().set_muted(!muted);
            pump_now(&app);
        }
    });
    window.on_link_clicked(|url| open_link(&url));
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
    } else if let Some(path) = shot {
        driver::start_shot(path, arg("--select").unwrap_or_default());
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
