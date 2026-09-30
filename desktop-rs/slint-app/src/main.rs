//! The Clarp desktop in Slint: a thin view over `clarp-engine`. The engine
//! owns behaviour; this file turns its state into Slint models and the
//! window's actions into engine commands.
//!
//! `clarp-slint` opens the window. `--headless` runs it without a display on
//! Slint's software renderer (for checks), and `--e2e-out DIR` drives it like
//! a person would, saving a screenshot per stage (see `driver.rs`).

mod driver;
mod headless;

use clarp_core::presentation::PresentedRow;
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
    /// What each transcript row was built from, to update only changed rows.
    shown: RefCell<Vec<(PresentedRow, bool)>>,
    /// Messages whose tool calls the reader opened (not groups).
    expanded: RefCell<std::collections::HashSet<String>>,
}

pub fn app() -> Option<Rc<App>> {
    APP.with(|a| a.borrow().clone())
}

pub fn window() -> Option<AppWindow> {
    app()?.window.upgrade()
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

/// A composer chip; images show a thumbnail of the local file.
fn attachment(value: &serde_json::Value) -> Attachment {
    let text = |key: &str| value.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_owned();
    let local = if !text("local_source").is_empty() {
        text("local_source")
    } else if value.get("local").and_then(serde_json::Value::as_bool) == Some(true) {
        text("path")
    } else {
        String::new()
    };
    let thumbnail = if text("content_type").starts_with("image/") && !local.is_empty() {
        slint::Image::load_from_path(std::path::Path::new(&local)).unwrap_or_else(|_| {
            eprintln!("clarp-slint: no thumbnail for {local}");
            slint::Image::default()
        })
    } else {
        slint::Image::default()
    };
    let status = text("status");
    Attachment {
        id: text("id").into(),
        name: if text("name").is_empty() { "file".into() } else { text("name").into() },
        status: if status.is_empty() { "ready".into() } else { status.into() },
        thumbnail,
    }
}

/// Picks files to attach to the open chat. The chooser is the desktop's
/// portal dialog, opened off the UI thread; `CLARP_TEST_ATTACH_FILE` names
/// the file instead, so checks never open a chooser.
fn choose_attachment() {
    let Some(session) = app().map(|state| state.engine.borrow().selected_session().to_owned()) else { return };
    if session.is_empty() {
        return;
    }
    let attach = move |paths: Vec<std::path::PathBuf>| {
        let Some(app) = app() else { return };
        for path in paths {
            app.engine.borrow_mut().attach_file(&session, &path);
        }
        pump_now(&app);
    };
    if let Some(path) = std::env::var_os("CLARP_TEST_ATTACH_FILE") {
        attach(vec![path.into()]);
        return;
    }
    let spawned = std::thread::Builder::new().name("attach-dialog".into()).spawn(move || {
        let chosen = rfd::FileDialog::new().set_title("Attach files").pick_files().unwrap_or_default();
        if chosen.is_empty() {
            return;
        }
        if let Err(error) = slint::invoke_from_event_loop(move || attach(chosen)) {
            eprintln!("clarp-slint: dropped the chosen files: {error}");
        }
    });
    if let Err(error) = spawned {
        eprintln!("clarp-slint: cannot open the attach dialog: {error}");
    }
}

/// Brings the transcript model to `rows` touching only what changed: the
/// rows kept at the start and end are updated in place (and only when their
/// source differs), so streaming does not rebuild the list and an older page
/// is inserted above rather than replacing everything.
fn sync_rows(model: &VecModel<MessageRow>, shown: &mut Vec<(PresentedRow, bool)>, fresh: Vec<(PresentedRow, bool)>, rows: Vec<MessageRow>) {
    use slint::Model;
    let id = |row: &(PresentedRow, bool)| row.0.message.id.clone();
    let prefix = shown.iter().zip(&fresh).take_while(|(a, b)| id(a) == id(b)).count();
    let most = shown.len().min(fresh.len()) - prefix;
    let suffix = shown.iter().rev().zip(fresh.iter().rev()).take(most).take_while(|(a, b)| id(a) == id(b)).count();
    let mut rows: Vec<Option<MessageRow>> = rows.into_iter().map(Some).collect();
    for index in (0..prefix).chain(fresh.len() - suffix..fresh.len()) {
        let old = if index < prefix { index } else { index + shown.len() - fresh.len() };
        if shown[old] != fresh[index] {
            model.set_row_data(old, rows[index].take().expect("each row is used once"));
        }
    }
    let (old_middle, new_middle) = (shown.len() - prefix - suffix, fresh.len() - prefix - suffix);
    for _ in 0..old_middle {
        model.remove(prefix);
    }
    for (offset, row) in rows[prefix..prefix + new_middle].iter_mut().enumerate() {
        model.insert(prefix + offset, row.take().expect("each row is used once"));
    }
    *shown = fresh;
}

fn tool_row(tool: &serde_json::Value) -> ToolRow {
    let text = |key: &str| tool.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_owned();
    let first = |keys: &[&str], fallback: &str| {
        keys.iter().map(|k| text(k)).find(|v| !v.is_empty()).unwrap_or_else(|| fallback.to_owned())
    };
    let mut detail = Vec::new();
    if !text("command").is_empty() {
        detail.push(text("command"));
    } else if let Some(input) = tool.get("input") {
        detail.push(input.as_str().map_or_else(|| serde_json::to_string_pretty(input).unwrap_or_default(), str::to_owned));
    }
    if !text("result").is_empty() {
        detail.push(text("result"));
    }
    ToolRow {
        name: first(&["name", "action"], "Tool").into(),
        summary: first(&["summary", "description", "file_path"], "").into(),
        status: first(&["status"], "recorded").into(),
        detail: detail.join("\n\n").into(),
    }
}

/// "HH:MM" in local time for an RFC 3339 stamp.
fn message_stamp(timestamp: &str) -> String {
    chrono::DateTime::parse_from_rfc3339(timestamp)
        .map(|t| t.with_timezone(&chrono::Local).format("%H:%M").to_string())
        .unwrap_or_default()
}

fn message_row(
    row: &clarp_core::presentation::PresentedRow,
    always_show_tools: bool,
    expanded: &std::collections::HashSet<String>,
) -> MessageRow {
    let m = &row.message;
    let author = if row.activity { "activity" } else if m.role == "user" { "user" } else { "assistant" };
    let text = if row.body.is_empty() && !row.activity { m.text.clone() } else { row.body.clone() };
    // The user's own words stay literal; replies are Markdown.
    let blocks = if author == "user" {
        vec![clarp_engine::blocks::Block::Prose(text.clone())]
    } else {
        clarp_engine::blocks::blocks(&text)
    };
    let from_agent = author == "user" && m.origin == "agent" && !m.sender_name.is_empty();
    let count = (row.activity_count as usize).max(row.tools.len());
    let has_activity = !row.activity && count > 0;
    let inline = always_show_tools || row.activity_inline;
    let label = if !has_activity || inline {
        String::new()
    } else if !row.group_label.is_empty() {
        row.group_label.clone()
    } else if !row.activity_label.is_empty() {
        row.activity_label.clone()
    } else {
        format!("{count} tool call{}", if count == 1 { "" } else { "s" })
    };
    // A group is keyed by its first member (presentation `toggle_group`).
    let group_id = if row.group_label.is_empty() { String::new() } else { m.id.clone() };
    let open = has_activity && (inline || if group_id.is_empty() { expanded.contains(&m.id) } else { row.group_expanded });
    MessageRow {
        id: m.id.clone().into(),
        author: author.into(),
        blocks: ModelRc::new(VecModel::from(blocks.iter().map(|b| message_block(b, author == "user")).collect::<Vec<_>>())),
        sender: if from_agent { m.sender_name.clone() } else { String::new() }.into(),
        stamp: message_stamp(&m.timestamp).into(),
        meta: SharedString::new(),
        pending: m.pending,
        failed: m.delivery_failed,
        activity_label: label.into(),
        group_id: group_id.into(),
        expanded: open,
        tools: ModelRc::new(VecModel::from(row.tools.iter().map(tool_row).collect::<Vec<_>>())),
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
        let conversation_changed =
            changes.iter().any(|c| matches!(c, Change::Selection | Change::Preferences) || matches!(c, Change::Conversation(s) if *s == selected));
        drop(engine);
        if conversation_changed {
            let presented = self.engine.borrow_mut().presented(&selected);
            let always = self.engine.borrow().activity_mode() == clarp_core::presentation::ALWAYS_VISIBLE;
            let expanded = self.expanded.borrow();
            let rows: Vec<MessageRow> = presented.iter().map(|row| message_row(row, always, &expanded)).collect();
            // Opened activity the Host sent without its tool calls: fetch them.
            let mut engine = self.engine.borrow_mut();
            for (row, shown) in presented.iter().zip(&rows) {
                if !shown.expanded {
                    continue;
                }
                if row.group_label.is_empty() {
                    if row.message.tool_details_available {
                        engine.load_tool_details(&selected, &row.message.id);
                    }
                } else {
                    for id in &row.group_ids {
                        engine.load_tool_details(&selected, id);
                    }
                }
            }
            drop(engine);
            let fresh: Vec<(PresentedRow, bool)> = presented.into_iter().zip(rows.iter().map(|r| r.expanded)).collect();
            sync_rows(&self.messages, &mut self.shown.borrow_mut(), fresh, rows);
            if changes.iter().any(|c| matches!(c, Change::Selection)) {
                window.invoke_transcript_to_latest();
            }
        }
        let engine = self.engine.borrow();
        let composer_changed = changes
            .iter()
            .any(|c| matches!(c, Change::Selection | Change::Roster) || matches!(c, Change::Composer(s) if *s == selected));
        if composer_changed {
            let attachments: Vec<Attachment> = engine.attachments(&selected).iter().map(attachment).collect();
            window.set_attachments(ModelRc::new(VecModel::from(attachments)));
            window.set_can_send(engine.can_send(&selected));
            window.set_queued(engine.queue_count(&selected));
            window.set_quota_notice(engine.quota_notice(&selected).into());
        }
        if changes.contains(&Change::Selection) {
            // The draft is the chat's: the editor shows the one it left.
            window.set_draft(engine.draft(&selected).into());
            // A chat opens ready to type into, unless the reader is scrolling.
            if !window.get_transcript_focused() {
                window.invoke_focus_composer();
            }
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
    let check = arg("--check");
    let headless = e2e_out.is_some() || shot.is_some() || check.is_some() || args.iter().any(|a| a == "--headless");
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
        shown: RefCell::new(Vec::new()),
        expanded: RefCell::new(std::collections::HashSet::new()),
    });
    APP.with(|a| *a.borrow_mut() = Some(state.clone()));

    window.on_chat_chosen(|session| {
        if let Some(app) = app() {
            app.engine.borrow_mut().select(&session);
            pump_now(&app);
        }
    });
    window.on_send(|text, queue| {
        if let Some(app) = app() {
            let selected = app.engine.borrow().selected_session().to_owned();
            let sent = app.engine.borrow_mut().send_composer(&selected, &text, queue);
            if sent && let Some(window) = app.window.upgrade() {
                window.set_draft(SharedString::new());
                // Your own message always brings the latest into view.
                window.invoke_transcript_to_latest();
            }
            pump_now(&app);
        }
    });
    window.on_draft_edited(|text| {
        if let Some(app) = app() {
            let selected = app.engine.borrow().selected_session().to_owned();
            app.engine.borrow_mut().set_draft(&selected, &text);
        }
    });
    window.on_remove_attachment(|id| {
        if let Some(app) = app() {
            let selected = app.engine.borrow().selected_session().to_owned();
            app.engine.borrow_mut().remove_attachment(&selected, &id);
            pump_now(&app);
        }
    });
    window.on_attach(choose_attachment);
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
    window.on_load_older(|| {
        if let Some(app) = app() {
            let selected = app.engine.borrow().selected_session().to_owned();
            app.engine.borrow_mut().load_older(&selected);
            pump_now(&app);
        }
    });
    window.on_toggle_activity(|id, group| {
        if let Some(app) = app() {
            if group.is_empty() {
                let id = id.to_string();
                let mut expanded = app.expanded.borrow_mut();
                if !expanded.remove(&id) {
                    expanded.insert(id);
                }
            } else {
                app.engine.borrow_mut().toggle_group(&group);
            }
            pump_now(&app);
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
    } else if let Some(name) = check {
        driver::start_check(&name, arg("--out").unwrap_or_else(|| ".".into()));
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
/// Shows what a command just changed, without waiting for the wake.
fn pump_now(app: &Rc<App>) {
    let mut changes = app.engine.borrow_mut().pump();
    let selected = app.engine.borrow().selected_session().to_owned();
    changes.push(Change::Conversation(selected));
    app.refresh(&changes);
}
