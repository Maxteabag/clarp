//! Engine state as Slint rows: chats, messages, tool cards, attachments,
//! and the reading theme's palette.

use clarp_core::presentation::PresentedRow;
use clarp_core::reading_theme::{FontFamily, FontOverride, ResolvedFont};
use slint::{ModelRc, SharedString, VecModel};

use crate::{AppWindow, Attachment, ChatRow, MessageBlock, MessageRow, Palette, TableRow, ToolRow, WidgetPalette};
use slint::ComponentHandle;

pub(crate) fn color(value: &str) -> Option<slint::Color> {
    let hex = value.strip_prefix('#')?;
    let n = u32::from_str_radix(hex, 16).ok()?;
    Some(match hex.len() {
        6 => slint::Color::from_rgb_u8((n >> 16) as u8, (n >> 8) as u8, n as u8),
        8 => slint::Color::from_argb_u8((n >> 24) as u8, (n >> 16) as u8, (n >> 8) as u8, n as u8),
        _ => return None,
    })
}

/// The installed fonts, as `fc-list` lists them.
pub(crate) struct Fonts {
    /// Every name a family answers to (its other names too).
    names: std::collections::HashSet<String>,
    /// The families, once each, for the font picker.
    list: Vec<FontFamily>,
}

thread_local! {
    /// Installed font families, once `fc-list` has answered.
    static FONTS: std::cell::RefCell<Option<Fonts>> = const { std::cell::RefCell::new(None) };
}

/// Asks fontconfig for the installed families and whether they are
/// monospace.
fn list_fonts() -> Result<Fonts, String> {
    let output = std::process::Command::new("fc-list")
        .args(["-f", "%{family}\\t%{spacing}\\n"])
        .output()
        .map_err(|error| format!("cannot list fonts: {error}"))?;
    if !output.status.success() {
        return Err(format!("fc-list failed: {}", String::from_utf8_lossy(&output.stderr).trim()));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let names = text
        .lines()
        .flat_map(|line| line.split('\t').next().unwrap_or_default().split(',').map(|f| f.trim().to_owned()).collect::<Vec<_>>())
        .filter(|f| !f.is_empty())
        .collect();
    Ok(Fonts { names, list: clarp_core::reading_theme::parse_font_list(&text) })
}

/// Reads the installed families off the UI thread, then applies the theme
/// again with the family it can really use (`chosen` is the reader's own,
/// for a window without its app yet).
pub(crate) fn load_fonts(theme: String, chosen: Option<FontOverride>) {
    let spawned = std::thread::Builder::new().name("font-list".into()).spawn(move || {
        let fonts = match list_fonts() {
            Ok(fonts) => fonts,
            Err(error) => {
                eprintln!("clarp-slint: {error}");
                return;
            }
        };
        let applied = slint::invoke_from_event_loop(move || {
            FONTS.with(|f| *f.borrow_mut() = Some(fonts));
            if let Some(window) = crate::window() {
                match crate::app() {
                    Some(app) => apply_app_theme(&app, &window),
                    None => apply_theme(&window, &theme, chosen.as_ref()),
                }
            }
        });
        if let Err(error) = applied {
            eprintln!("clarp-slint: dropped the font list: {error}");
        }
    });
    if let Err(error) = spawned {
        eprintln!("clarp-slint: cannot list fonts: {error}");
    }
}

/// The installed families for the font picker; listed now if the
/// background listing has not answered yet.
pub(crate) fn font_list() -> Vec<FontFamily> {
    if let Some(list) = FONTS.with(|f| f.borrow().as_ref().map(|fonts| fonts.list.clone())) {
        return list;
    }
    match list_fonts() {
        Ok(fonts) => {
            let list = fonts.list.clone();
            FONTS.with(|f| *f.borrow_mut() = Some(fonts));
            list
        }
        Err(error) => {
            eprintln!("clarp-slint: {error}");
            Vec::new()
        }
    }
}

fn installed(family: &str) -> bool {
    FONTS.with(|fonts| fonts.borrow().as_ref().is_none_or(|f| f.names.contains(family)))
}

/// Whether `family` is a monospace family (unknown until the list loads).
fn monospace(family: &str) -> bool {
    family == "monospace" || FONTS.with(|fonts| fonts.borrow().as_ref().is_some_and(|f| f.list.iter().any(|l| l.monospace && l.name == family)))
}

/// The theme's first installed family (its first choice until the font
/// list is known).
pub(crate) fn theme_font(theme: &clarp_core::json::Object) -> String {
    clarp_core::reading_theme::resolve_font(theme, installed)
}

/// The font `theme` draws in with the reader's `chosen` one over it.
pub(crate) fn resolved_font(theme: &clarp_core::json::Object, chosen: Option<&FontOverride>) -> ResolvedFont {
    clarp_core::reading_theme::resolve(theme, chosen, installed)
}

/// The app's theme with the font the reader sees: the picker's while it is
/// open, else the theme's saved one.
pub(crate) fn apply_app_theme(app: &crate::App, window: &AppWindow) {
    let engine = app.engine.borrow();
    let theme = engine.reading_theme();
    let chosen = crate::font_view::previewed().or_else(|| engine.font_override(&theme));
    drop(engine);
    apply_theme(window, &theme, chosen.as_ref());
}

pub(crate) fn apply_theme(window: &AppWindow, id: &str, chosen: Option<&FontOverride>) {
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
    // The focused frame's colour: the theme's own, else its accent.
    if let Some(focus) = pick("focus").or_else(|| pick("accent")) {
        palette.set_focus(focus);
    }
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
    let font = resolved_font(theme, chosen);
    palette.set_body_size(font.size as f32);
    // Code keeps JetBrains Mono unless the reader chose a monospace family.
    let own = chosen.is_some_and(|c| !c.family.is_empty()) && font.missing.is_none();
    palette.set_code_family(if own && monospace(&font.family) { font.family.as_str() } else { "JetBrains Mono" }.into());
    palette.set_body_family(font.family.into());
    // A working row's shimmer, per tone.
    let shimmer = |tone: &str| crate::live_view::shimmer_colours(theme, tone).and_then(|(low, high)| Some((color(&low)?, color(&high)?)));
    match (shimmer("accent"), shimmer("mutedText")) {
        (Some((accent_low, accent_high)), Some((muted_low, muted_high))) => {
            palette.set_shimmer_accent_low(accent_low);
            palette.set_shimmer_accent_high(accent_high);
            palette.set_shimmer_muted_low(muted_low);
            palette.set_shimmer_muted_high(muted_high);
        }
        _ => eprintln!("clarp-slint: theme {id} has no shimmer colours"),
    }
    // The reader's own colours and size over the theme's.
    crate::look::over_theme(window, id);
}

thread_local! {
    /// Settings → Fold agent prompts: whether a prompt from another agent
    /// shows folded until it is opened (opening flips it either way).
    static PROMPTS_FOLDED: std::cell::Cell<bool> = const { std::cell::Cell::new(true) };
}

pub(crate) fn set_prompts_folded(folded: bool) {
    PROMPTS_FOLDED.with(|f| f.set(folded));
}

/// Whether the prompt `key` shows open: toggled from the default.
pub(crate) fn prompt_open(expanded: &std::collections::HashSet<String>, key: &str) -> bool {
    expanded.contains(key) == PROMPTS_FOLDED.with(std::cell::Cell::get)
}

/// Opens a local file with the desktop's app for it; checks record it to
/// `CLARP_TEST_OPEN_URL` instead.
pub(crate) fn open_file(path: &std::path::Path) -> Result<(), String> {
    if let Some(record) = std::env::var_os("CLARP_TEST_OPEN_URL") {
        use std::io::Write;
        let mut log = std::fs::OpenOptions::new().create(true).append(true).open(record).map_err(|e| e.to_string())?;
        return writeln!(log, "file://{}", path.display()).map_err(|e| e.to_string());
    }
    std::process::Command::new("xdg-open").arg(path).spawn().map(drop).map_err(|e| format!("could not open {}: {e}", path.display()))
}

pub(crate) fn stamp(epoch_millis: i64) -> String {
    if epoch_millis <= 0 {
        return String::new();
    }
    clarp_core::time_format::chat_stamp(epoch_millis, &chrono::Local::now())
}

pub(crate) fn initial(name: &str) -> SharedString {
    name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default().into()
}

/// Text for a one-line slot: runs of whitespace (newlines and tabs too)
/// become one space, since `overflow: elide` still draws every line.
fn one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(crate) fn chat_row(row: &clarp_core::roster::AgentRow, depth: usize, selected: &str) -> ChatRow {
    let preview = one_line(if !row.last_message.is_empty() { &row.last_message } else { &row.working_directory });
    let activity = if row.busy && !row.status_text.is_empty() { one_line(&row.status_text) } else { String::new() };
    let mut chat = ChatRow {
        session: row.session.clone().into(),
        initial: initial(&row.name),
        name: row.name.clone().into(),
        stamp: stamp(row.last_activity).into(),
        preview: preview.into(),
        activity: activity.into(),
        depth: depth as i32,
        queued: queued_shown(&row.session, row.queue_count),
        busy: row.busy,
        unread: row.unread,
        muted: row.muted,
        selected: row.session == selected,
        jobs: row.background_job_count,
        sub_agents: row.sub_agent_count,
        children: row.running_children,
        ..ChatRow::default()
    };
    (chat.running_helpers, chat.running_processes) = running_work(&chat, 0);
    chat
}

/// What an explorer row shows running: (sub-agents and helpers, background
/// processes). A helper is both a running child and its mirror job, so it
/// counts once; `nested` is the unfinished helpers listed under the row
/// (a finished one stays listed while its "helpers done" line is open).
pub(crate) fn running_work(chat: &ChatRow, nested: i32) -> (i32, i32) {
    let helpers = chat.sub_agents.max(chat.children).max(nested).max(0);
    (helpers, (chat.jobs - chat.sub_agents).max(0))
}

/// Inline Markdown as Slint styled text; plain text if Slint cannot parse it.
/// Bare web addresses are links, in literal text too (the user's own
/// words), so a click or a link hint opens them.
pub(crate) fn styled(markdown: &str, literal: bool) -> slint::StyledText {
    let plain = || slint::StyledText::from_plain_text(markdown);
    if literal {
        return clarp_engine::blocks::literal_with_links(markdown).and_then(|m| slint::StyledText::from_markdown(&m).ok()).unwrap_or_else(plain);
    }
    slint::StyledText::from_markdown(&clarp_engine::blocks::linkify(markdown)).or_else(|_| slint::StyledText::from_markdown(markdown)).unwrap_or_else(|_| plain())
}

pub(crate) fn message_block(block: &clarp_engine::blocks::Block, literal: bool) -> MessageBlock {
    use clarp_engine::blocks::Block;
    let empty = || ModelRc::new(VecModel::<TableRow>::default());
    match block {
        Block::Prose(markdown) => MessageBlock { kind: "prose".into(), styled: styled(markdown, literal), text: SharedString::new(), level: 0, rows: empty(), ..MessageBlock::default() },
        Block::Heading { level, markdown } => MessageBlock {
            kind: "heading".into(),
            styled: styled(&format!("**{markdown}**"), false),
            text: SharedString::new(),
            level: i32::from(*level),
            rows: empty(),
            ..MessageBlock::default()
        },
        Block::Code { text, .. } => MessageBlock { kind: "code".into(), styled: slint::StyledText::default(), text: text.clone().into(), level: 0, rows: empty(), ..MessageBlock::default() },
        Block::Quote(markdown) => MessageBlock { kind: "quote".into(), styled: styled(markdown, false), text: SharedString::new(), level: 0, rows: empty(), ..MessageBlock::default() },
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
            ..MessageBlock::default()
        },
        Block::Rule => MessageBlock { kind: "rule".into(), styled: slint::StyledText::default(), text: SharedString::new(), level: 0, rows: empty(), ..MessageBlock::default() },
    }
}

/// Only web and mail links open, in the desktop's browser; tests record
/// them to `CLARP_TEST_OPEN_URL` instead.
pub(crate) fn open_link(url: &str) {
    let openable = url.starts_with("https://") || url.starts_with("http://") || url.starts_with("mailto:");
    if !openable {
        eprintln!("clarp-slint: not opening {url}: only web and mail links open");
        return;
    }
    // Settings → Opening links: copy the address instead.
    let copy = crate::app().is_some_and(|app| app.engine.try_borrow().is_ok_and(|e| clarp_core::prefs::choice_of(e.settings(), "linkopen") == "copy"));
    if copy {
        if let Err(error) = crate::platform::clipboard::copy(url) {
            eprintln!("clarp-slint: could not copy {url}: {error}");
        }
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
pub(crate) fn attachment(value: &serde_json::Value) -> Attachment {
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
pub(crate) fn choose_attachment(session: String) {
    use crate::{app, pump_now};
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
/// A shown row's source: the presented row, whether it is open, its
/// artifacts' ids and revisions, and its place: the key the list keeps it
/// by (its id, a live item's key, or for a durable row that took over
/// live items the key of the row whose place it took).
pub(crate) type Shown = (PresentedRow, bool, String, String);

thread_local! {
    /// Rows the transcripts updated in place and inserted, since start.
    static SYNC_STATS: std::cell::Cell<(usize, usize)> = const { std::cell::Cell::new((0, 0)) };
}

/// (updated in place, inserted) rows across the transcripts so far.
pub(crate) fn sync_stats() -> (usize, usize) {
    SYNC_STATS.with(std::cell::Cell::get)
}

pub(crate) fn sync_rows(model: &VecModel<MessageRow>, shown: &mut Vec<Shown>, fresh: Vec<Shown>, rows: Vec<MessageRow>) {
    use slint::Model;
    let id = |row: &Shown| row.3.clone();
    let prefix = shown.iter().zip(&fresh).take_while(|(a, b)| id(a) == id(b)).count();
    let most = shown.len().min(fresh.len()) - prefix;
    let suffix = shown.iter().rev().zip(fresh.iter().rev()).take(most).take_while(|(a, b)| id(a) == id(b)).count();
    let mut rows: Vec<Option<MessageRow>> = rows.into_iter().map(Some).collect();
    for index in (0..prefix).chain(fresh.len() - suffix..fresh.len()) {
        let old = if index < prefix { index } else { index + shown.len() - fresh.len() };
        if shown[old] != fresh[index] {
            model.set_row_data(old, rows[index].take().expect("each row is used once"));
            SYNC_STATS.with(|s| s.set((s.get().0 + 1, s.get().1)));
        }
    }
    let (old_middle, new_middle) = (shown.len() - prefix - suffix, fresh.len() - prefix - suffix);
    for _ in 0..old_middle {
        model.remove(prefix);
    }
    for (offset, row) in rows[prefix..prefix + new_middle].iter_mut().enumerate() {
        model.insert(prefix + offset, row.take().expect("each row is used once"));
        SYNC_STATS.with(|s| s.set((s.get().0, s.get().1 + 1)));
    }
    *shown = fresh;
}

pub(crate) fn tool_row(tool: &serde_json::Value) -> ToolRow {
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
        ..ToolRow::default()
    }
}

/// How long a message of one's own may stay unsent before its bubble says
/// so ("Sending…"): the iPhone's 1.75 s (clarp-ios 7bdc0d3). Most sends are
/// confirmed well within it: the bubble is drawn at its final size straight
/// away instead of with a status line that is gone a moment later. A send
/// that will not be confirmed shows at once: the desktop never retries one
/// by itself, so that is a failed send ("Not delivered", on an error or the
/// delivery timeout).
pub(crate) const SEND_STATUS_GRACE: std::time::Duration = std::time::Duration::from_millis(1750);

/// Queued-turn labels (the composer's line, the explorer's and overview's
/// counts) wait the same grace period before a rise shows: a send queued
/// behind a turn is most often taken at once. A fall shows at once, and a
/// chat's count is shown as it is the first time it is seen.
#[derive(Default)]
pub(crate) struct QueueGrace {
    /// Session → the count shown, and since when a higher one has waited.
    shown: std::collections::HashMap<String, (i32, Option<std::time::Instant>)>,
}

impl QueueGrace {
    /// The count to show for `actual` at `now`, and how long until a rise
    /// still waiting is due (to look again then).
    pub(crate) fn shown(&mut self, session: &str, actual: i32, now: std::time::Instant) -> (i32, Option<std::time::Duration>) {
        let entry = self.shown.entry(session.to_owned()).or_insert((actual, None));
        if actual <= entry.0 {
            *entry = (actual, None);
            return (actual, None);
        }
        let since = *entry.1.get_or_insert(now);
        match send_status_due(now.duration_since(since)) {
            Ok(()) => {
                *entry = (actual, None);
                (actual, None)
            }
            Err(left) => (entry.0, Some(left)),
        }
    }
}

thread_local! {
    static QUEUE_GRACE: std::cell::RefCell<QueueGrace> = std::cell::RefCell::default();
    /// Sessions with a refresh already set for a waiting rise.
    static QUEUE_DUE: std::cell::RefCell<std::collections::HashSet<String>> = std::cell::RefCell::default();
}

/// The queued-turn count `session`'s labels show (`QueueGrace`); a rise
/// still waiting brings the labels up to date once it is due.
pub(crate) fn queued_shown(session: &str, actual: i32) -> i32 {
    let (shown, wait) = QUEUE_GRACE.with(|g| g.borrow_mut().shown(session, actual, std::time::Instant::now()));
    if let Some(left) = wait
        && QUEUE_DUE.with(|d| d.borrow_mut().insert(session.to_owned()))
    {
        let session = session.to_owned();
        slint::Timer::single_shot(left + std::time::Duration::from_millis(10), move || {
            QUEUE_DUE.with(|d| d.borrow_mut().remove(&session));
            if let Some(app) = crate::app() {
                app.refresh(&[clarp_engine::Change::Roster, clarp_engine::Change::Composer(session)]);
            }
        });
    }
    shown
}

/// Whether an unsent message's status is due after `pending_for`, or how
/// long until it is.
pub(crate) fn send_status_due(pending_for: std::time::Duration) -> Result<(), std::time::Duration> {
    if pending_for >= SEND_STATUS_GRACE { Ok(()) } else { Err(SEND_STATUS_GRACE - pending_for) }
}

/// "HH:MM" in local time for an RFC 3339 stamp.
pub(crate) fn message_stamp(timestamp: &str) -> String {
    chrono::DateTime::parse_from_rfc3339(timestamp)
        .map(|t| t.with_timezone(&chrono::Local).format("%H:%M").to_string())
        .unwrap_or_default()
}

pub(crate) fn message_row(
    row: &clarp_core::presentation::PresentedRow,
    always_show_tools: bool,
    expanded: &std::collections::HashSet<String>,
) -> MessageRow {
    let m = &row.message;
    let author = if row.activity { "activity" } else if m.role == "user" { "user" } else { "assistant" };
    // A reply is only ever its written form: a row whose voice markup was
    // all there was shows nothing rather than the tags.
    let text = if row.body.is_empty() && !row.activity && author == "user" { m.text.clone() } else { row.body.clone() };
    let receipt = if author == "user" { receipt_row(m) } else { None };
    // Another agent's message folds to one line until opened by its key
    // (a resolved decision is its receipt instead).
    let prompt = if author == "user" && receipt.is_none() { clarp_core::agent_prompt::of(m) } else { None };
    let prompt_open = prompt.as_ref().is_some_and(|p| prompt_open(expanded, &p.key));
    // The user's own words stay literal; replies and opened prompts are
    // Markdown. A resolved decision is its receipt, never the Host's prompt.
    let blocks = match &prompt {
        _ if receipt.is_some() => Vec::new(),
        Some(_) if !prompt_open => Vec::new(),
        None if author == "user" => vec![clarp_engine::blocks::Block::Prose(text.clone())],
        _ => clarp_engine::blocks::blocks(&text),
    };
    let count = (row.activity_count as usize).max(row.tools.len()).max(row.display_cells.len());
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
        blocks: ModelRc::new(VecModel::from({
            let mut shown: Vec<crate::MessageBlock> = blocks.iter().flat_map(|b| crate::artifacts_view::with_images(b, author == "user" && prompt.is_none())).collect();
            // Image blocks the keyboard reaches (J/K) and enlarges: not the
            // web's, which are never fetched.
            for (index, block) in shown.iter_mut().enumerate() {
                if block.kind == "images" && slint::Model::iter(&block.images).any(|i| !i.foreign) {
                    block.key = crate::artifacts_view::image_key(&m.id, index).into();
                }
            }
            shown
        })),
        stamp: message_stamp(&m.timestamp).into(),
        meta: SharedString::new(),
        pending: m.pending,
        // The pane decides once the grace period has passed.
        status_due: false,
        failed: m.delivery_failed,
        activity_label: label.into(),
        group_id: group_id.into(),
        expanded: open,
        tools: ModelRc::new(VecModel::from(
            row.tools.iter().filter(|t| crate::cells_view::shows_tool(row, t)).map(tool_row).collect::<Vec<_>>(),
        )),
        cells: ModelRc::new(VecModel::from(crate::cells_view::cells(row))),
        artifacts: ModelRc::new(VecModel::<crate::ArtifactItem>::default()),
        live: crate::LiveRow::default(),
        receipt: receipt.unwrap_or_default(),
        prompt: prompt.map_or_else(crate::PromptRow::default, |p| crate::PromptRow {
            initial: p.sender.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default().into(),
            key: p.key.into(),
            sender: p.sender.into(),
            line: p.line.into(),
            expanded: prompt_open,
        }),
    }
}

/// The receipt a resolved-decision prompt shows as (its agent, kind and
/// whether its card is in the chat are the pane's to fill in).
pub(crate) fn receipt_row(m: &clarp_core::protocol::Message) -> Option<crate::ReceiptRow> {
    let receipt = clarp_core::decision_receipt::parse(&m.text)?;
    let kind = if matches!(receipt.outcome, clarp_core::decision_receipt::Outcome::Answered(_)) { "QUESTION" } else { "DECISION" };
    Some(crate::ReceiptRow {
        key: receipt_key(&m.id).into(),
        question: receipt.question.into(),
        mark: receipt.outcome.mark().into(),
        outcome: receipt.outcome.label().into(),
        tone: receipt.outcome.tone().into(),
        kind: kind.into(),
        agent: SharedString::new(),
        artifact_id: receipt.artifact_id.into(),
        linked: false,
    })
}

/// A receipt's key: J/K and link hints reach it by its message.
pub(crate) fn receipt_key(message_id: &str) -> String {
    format!("receipt:{message_id}")
}

/// What a cached row was built from: an equal source builds an equal row.
#[derive(PartialEq)]
struct RowSource {
    row: PresentedRow,
    always: bool,
    open: bool,
    /// Another agent's prompt is open (by its key).
    prompt_open: bool,
    /// Rows with images are built again when a picture lands.
    pictures: Option<u64>,
}

/// Each transcript row as `message_row` built it (Markdown blocks, styled
/// text, tools and cells), kept per message id while its presented source
/// is unchanged, so an update re-parses only the rows that changed: a
/// streamed reply's tick rebuilds one row, not the whole chat.
#[derive(Default)]
pub(crate) struct RowCache {
    rows: std::collections::HashMap<String, (RowSource, MessageRow)>,
    /// Live item rows (docs/live-items.md): key → the entry's signature
    /// (revision, status, revealed text, …) and the row built from it.
    live: std::collections::HashMap<String, (String, MessageRow)>,
    /// Rows built (not reused) so far.
    pub(crate) built: u64,
}

impl RowCache {
    pub(crate) fn clear(&mut self) {
        self.rows.clear();
        self.live.clear();
    }

    /// A live item's row, built again only when its signature changed (a
    /// streaming message's blocks then come from the committed-block cache,
    /// so only its tail is parsed).
    pub(crate) fn live_row(&mut self, entry: &clarp_core::live_present::Entry) -> MessageRow {
        let signature = crate::live_view::signature(entry);
        if let Some((seen, row)) = self.live.get(&entry.key)
            && *seen == signature
        {
            return row.clone();
        }
        self.built += 1;
        let row = crate::live_view::row(entry, crate::live_view::cached_blocks(entry));
        self.live.insert(entry.key.clone(), (signature, row.clone()));
        row
    }

    /// Drops live rows no longer shown.
    pub(crate) fn keep_live(&mut self, keys: &std::collections::HashSet<&str>) {
        self.live.retain(|key, _| keys.contains(key.as_str()));
    }

    pub(crate) fn rows(&mut self, presented: &[PresentedRow], always: bool, expanded: &std::collections::HashSet<String>) -> Vec<MessageRow> {
        let pictures = crate::artifacts_view::pictures_landed();
        let mut kept = std::collections::HashMap::with_capacity(presented.len());
        let rows = presented
            .iter()
            .map(|row| {
                let id = row.message.id.clone();
                let open = expanded.contains(&id);
                let prompt_open = row.message.origin == "agent" && prompt_open(expanded, &clarp_core::agent_prompt::key(&id));
                let reused = self.rows.remove(&id).filter(|(source, _)| {
                    source.always == always
                        && source.open == open
                        && source.prompt_open == prompt_open
                        && source.pictures.is_none_or(|p| p == pictures)
                        && source.row == *row
                });
                let (source, shown) = reused.unwrap_or_else(|| {
                    self.built += 1;
                    let shown = message_row(row, always, expanded);
                    let images = slint::Model::iter(&shown.blocks).any(|b| b.kind == "images");
                    (RowSource { row: row.clone(), always, open, prompt_open, pictures: images.then_some(pictures) }, shown)
                });
                let copy = shown.clone();
                kept.insert(id, (source, shown));
                copy
            })
            .collect();
        self.rows = kept;
        rows
    }
}

#[cfg(test)]
mod tests {
    /// The explorer's preview and activity lines are one line each: a helper
    /// running a multi-line command must not spill over the next chat.
    #[test]
    fn an_unsent_message_says_so_only_after_the_grace_period() {
        use std::time::Duration;
        assert_eq!(super::send_status_due(Duration::ZERO), Err(super::SEND_STATUS_GRACE));
        assert_eq!(super::send_status_due(Duration::from_millis(1000)), Err(super::SEND_STATUS_GRACE - Duration::from_millis(1000)));
        assert_eq!(super::send_status_due(super::SEND_STATUS_GRACE), Ok(()));
        assert_eq!(super::send_status_due(Duration::from_secs(5)), Ok(()));
    }

    #[test]
    fn a_queued_count_rises_only_after_the_grace_period_and_falls_at_once() {
        use std::time::{Duration, Instant};
        let mut grace = super::QueueGrace::default();
        let start = Instant::now();
        assert_eq!(grace.shown("a", 2, start), (2, None), "a chat's count first seen shows as it is");
        assert_eq!(grace.shown("a", 3, start), (2, Some(super::SEND_STATUS_GRACE)), "a rise waits");
        let later = start + Duration::from_millis(1000);
        assert_eq!(grace.shown("a", 3, later), (2, Some(super::SEND_STATUS_GRACE - Duration::from_millis(1000))));
        assert_eq!(grace.shown("a", 3, start + super::SEND_STATUS_GRACE), (3, None), "and shows once due");
        assert_eq!(grace.shown("a", 0, start + super::SEND_STATUS_GRACE), (0, None), "a fall shows at once");
        assert_eq!(grace.shown("a", 1, start + Duration::from_secs(10)), (0, Some(super::SEND_STATUS_GRACE)), "a new rise waits again");
        assert_eq!(grace.shown("a", 0, start + Duration::from_secs(11)), (0, None), "taken within the grace period: never shown");
    }

    #[test]
    fn explorer_lines_stay_on_one_line() {
        let row = clarp_core::roster::AgentRow {
            session: "jasper".into(),
            name: "Jasper".into(),
            busy: true,
            status_text: "helper: python3 - <<'EOF'\np = 'core/tests/x.rs'\n\tassert_eq!(a, b)".into(),
            last_message: "First line\n\nsecond line".into(),
            ..Default::default()
        };
        let chat = super::chat_row(&row, 0, "");
        assert_eq!(chat.activity.as_str(), "helper: python3 - <<'EOF' p = 'core/tests/x.rs' assert_eq!(a, b)");
        assert_eq!(chat.preview.as_str(), "First line second line");
    }

    /// The compact explorer's marks count what the full row's badges count:
    /// nothing, a helper (its mirror job and its running child are one), two
    /// processes, or both.
    #[test]
    fn running_work_counts_helpers_and_processes() {
        let row = |jobs, sub_agents, children| crate::ChatRow { jobs, sub_agents, children, ..Default::default() };
        assert_eq!(super::running_work(&row(0, 0, 0), 0), (0, 0), "nothing runs");
        assert_eq!(super::running_work(&row(1, 1, 1), 0), (1, 0), "one helper");
        assert_eq!(super::running_work(&row(0, 0, 0), 1), (1, 0), "one helper listed under the row");
        assert_eq!(super::running_work(&row(2, 0, 0), 0), (0, 2), "two processes");
        assert_eq!(super::running_work(&row(3, 1, 0), 2), (2, 2), "both");
    }

    use clarp_core::protocol::Message;

    /// An agent's reply with code blocks inside a numbered list (a real one,
    /// anonymised) must show as Markdown, not fall back to raw plain text.
    #[test]
    fn a_reply_with_code_inside_a_list_renders_as_markdown() {
        // As the Host sends it: the app cleans the voice tags out on parsing.
        let json = serde_json::json!({"id": "a1", "role": "assistant", "text": include_str!("../tests/fixtures/nested-code-reply.md")});
        let message = Message::from_json(json.as_object().expect("an object"));
        let presented = clarp_core::presentation::present(&[message], &mut clarp_core::presentation::Settings::default(), None);
        let row = presented.rows.first().expect("the reply is presented");
        let text = if row.body.is_empty() { row.message.text.clone() } else { row.body.clone() };
        let blocks = clarp_engine::blocks::blocks(&text);
        let codes = blocks.iter().filter(|b| matches!(b, clarp_engine::blocks::Block::Code { .. })).count();
        assert_eq!(codes, 2, "both commands are code blocks: {blocks:#?}");
        for block in &blocks {
            if let clarp_engine::blocks::Block::Prose(markdown) = block {
                let parsed = slint::StyledText::from_markdown(markdown);
                assert!(parsed.is_ok(), "Slint parses this prose as Markdown ({:?}):\n{markdown}", parsed.err().map(|e| e.to_string()));
            }
        }
    }

    /// Bare web addresses become links Slint can parse, in replies and in
    /// the user's literal text (escaped punctuation, kept blank lines).
    #[test]
    fn bare_addresses_parse_as_links() {
        let reply = clarp_engine::blocks::linkify("Raw: https://bare.example/path?x=1. and [guide](https://example.com/guide)");
        assert!(slint::StyledText::from_markdown(&reply).is_ok(), "{reply}");
        let literal = clarp_engine::blocks::literal_with_links("Check https://user.example/page *now*\n\n  1. [x](y) <b> # `c`\n> q").expect("a link");
        let parsed = slint::StyledText::from_markdown(&literal);
        assert!(parsed.is_ok(), "{literal:?}: {:?}", parsed.err().map(|e| e.to_string()));
    }

    /// A streamed reply's tick re-parses its own row only; the rest of a
    /// long chat is reused as it was.
    #[test]
    fn an_update_rebuilds_only_the_rows_that_changed() {
        let message = |id: &str, role: &str, text: &str, revision: i64| {
            Message::from_json(serde_json::json!({"id": id, "role": role, "text": text, "revision": revision}).as_object().expect("an object"))
        };
        let mut messages: Vec<Message> = (0..30)
            .map(|i| message(&format!("m{i}"), if i % 2 == 0 { "user" } else { "assistant" }, &format!("Row {i} with **bold** and `code`"), i))
            .collect();
        messages.push(message("live", "assistant", "Partial", 31));
        let present = |messages: &[Message]| {
            clarp_core::presentation::present(messages, &mut clarp_core::presentation::Settings::default(), None).rows
        };
        let expanded = std::collections::HashSet::new();
        let mut cache = super::RowCache::default();
        let first = cache.rows(&present(&messages), false, &expanded);
        assert_eq!(cache.built, 31);
        messages[30] = message("live", "assistant", "Partial answer, growing", 32);
        let second = cache.rows(&present(&messages), false, &expanded);
        assert_eq!(cache.built, 32, "one row changed, one row built");
        assert_eq!(second.len(), first.len());
        assert!(first.iter().zip(&second).take(30).all(|(a, b)| a.id == b.id && slint::Model::row_count(&a.blocks) == slint::Model::row_count(&b.blocks)));
        let text = |row: &crate::MessageRow| slint::Model::row_data(&row.blocks, 0).map(|b| b.styled).unwrap_or_default();
        assert_eq!(text(&second[30]), super::styled("Partial answer, growing", false), "the changed row shows its new text");
        cache.rows(&present(&messages), true, &expanded);
        assert_eq!(cache.built, 63, "a presentation setting builds every row again");
    }

    fn prompt_rows(messages: &[serde_json::Value]) -> Vec<clarp_core::presentation::PresentedRow> {
        let messages: Vec<Message> = messages.iter().map(|m| Message::from_json(m.as_object().expect("an object"))).collect();
        clarp_core::presentation::present(&messages, &mut clarp_core::presentation::Settings::default(), None).rows
    }

    fn block_kinds(row: &crate::MessageRow) -> Vec<String> {
        slint::Model::iter(&row.blocks).map(|b| b.kind.to_string()).collect()
    }

    /// Another agent's message shows folded to one line, "Rachel prompted ·
    /// <first line>"; its key opens it to the whole message as Markdown.
    #[test]
    fn an_agents_prompt_is_one_line_until_opened() {
        let rows = prompt_rows(&[serde_json::json!({"id": "u7", "role": "user", "origin": "agent", "sender_name": "Rachel",
            "text": "## Build plan\n\nRun `cargo test` first.\n\n- then report"})]);
        let closed = super::message_row(&rows[0], false, &std::collections::HashSet::new());
        assert_eq!(closed.prompt.key, "a2a:u7");
        assert_eq!(closed.prompt.sender, "Rachel");
        assert_eq!(closed.prompt.initial, "R");
        assert_eq!(closed.prompt.line, "Build plan");
        assert!(!closed.prompt.expanded, "folded by default");
        let open = super::message_row(&rows[0], false, &["a2a:u7".to_owned()].into_iter().collect());
        assert!(open.prompt.expanded);
        assert_eq!(block_kinds(&open), ["heading", "prose"], "opened, the whole message as Markdown");
        let markdown: Vec<_> = clarp_engine::blocks::blocks("## Build plan\n\nRun `cargo test` first.\n\n- then report")
            .iter()
            .map(|b| super::message_block(b, false).styled)
            .collect();
        let shown: Vec<_> = slint::Model::iter(&open.blocks).map(|b| b.styled).collect();
        assert_eq!(shown, markdown, "styled as a reply is, not kept literal as the user's own words");
    }

    /// The user's own words and the agent's replies are not folded.
    #[test]
    fn own_messages_and_replies_are_not_prompts() {
        let rows = prompt_rows(&[
            serde_json::json!({"id": "u1", "role": "user", "text": "Hello\nthere"}),
            serde_json::json!({"id": "a1", "role": "assistant", "text": "Hi"}),
        ]);
        for row in &rows {
            let shown = super::message_row(row, false, &std::collections::HashSet::new());
            assert_eq!(shown.prompt.key, "", "{}", shown.id);
        }
    }

    /// Opening a prompt builds its row again (the cache never shows a stale
    /// fold); the other rows are reused.
    #[test]
    fn a_prompts_open_state_is_kept_by_id_through_the_cache() {
        let rows = prompt_rows(&[
            serde_json::json!({"id": "u1", "role": "user", "text": "Hello"}),
            serde_json::json!({"id": "u7", "role": "user", "origin": "agent", "sender_name": "Rachel", "text": "Line one\nLine two"}),
        ]);
        let mut cache = super::RowCache::default();
        let mut expanded = std::collections::HashSet::new();
        assert!(!cache.rows(&rows, false, &expanded)[1].prompt.expanded);
        expanded.insert("a2a:u7".to_owned());
        let open = cache.rows(&rows, false, &expanded);
        assert!(open[1].prompt.expanded, "opened by its key");
        assert_eq!(cache.built, 3, "only the prompt is built again");
        assert!(cache.rows(&rows, false, &expanded)[1].prompt.expanded, "and stays open when presented again");
        expanded.remove("a2a:u7");
        assert!(!cache.rows(&rows, false, &expanded)[1].prompt.expanded, "folds again");
    }

    /// A live item's row is reused until its entry changes; a streaming
    /// message is built again per revealed step, each from the block cache.
    #[test]
    fn live_rows_are_built_again_only_when_their_entry_changes() {
        use clarp_core::live_present::{Entry, Kind};
        let mut cache = super::RowCache::default();
        let tool = Entry { key: "live:cl:t1".into(), kind: Kind::Tool, status: "running".into(), title: "Running npm test".into(), meta: "0:01".into(), rev: 1, ..Entry::default() };
        cache.live_row(&tool);
        cache.live_row(&tool);
        assert_eq!(cache.built, 1, "an unchanged entry reuses its row");
        let ticked = Entry { meta: "0:02".into(), ..tool.clone() };
        assert_eq!(cache.live_row(&ticked).live.meta, "0:02");
        assert_eq!(cache.built, 2, "a ticking elapsed time builds it again");
        let message = Entry { key: "live:cl:m".into(), kind: Kind::Message, text: "First.\n\nSecond".into(), rev: 3, ..Entry::default() };
        let row = cache.live_row(&message);
        assert_eq!(slint::Model::row_count(&row.blocks), 2);
        cache.keep_live(&["live:cl:m"].into_iter().collect());
        cache.live_row(&ticked);
        assert_eq!(cache.built, 4, "a dropped row is built afresh");
    }

    /// The Host's resolved-decision prompt (origin automation, role user)
    /// shows as its receipt: no protocol line reaches the chat.
    #[test]
    fn a_resolved_decision_shows_as_its_receipt_not_the_prompt() {
        let text = "[Clarp decision resolved]\nDecision ID: dec-7\nArtifact ID: art-42\nQuestion: Deploy to production?\nContext: CI is green.\nReference: \nPayload: {}\nThe user chose: rejected. Do not perform the protected action.";
        let messages = [Message::from_json(serde_json::json!({"id": "m1", "role": "user", "origin": "automation", "text": text}).as_object().unwrap())];
        let rows = clarp_core::presentation::present(&messages, &mut clarp_core::presentation::Settings::default(), None).rows;
        let row = super::message_row(&rows[0], false, &Default::default());
        assert_eq!(row.author, "user");
        assert_eq!(slint::Model::row_count(&row.blocks), 0, "no prompt text");
        assert_eq!(row.receipt.key, "receipt:m1");
        assert_eq!(row.receipt.question, "Deploy to production?");
        assert_eq!((row.receipt.outcome.as_str(), row.receipt.tone.as_str(), row.receipt.mark.as_str()), ("Declined", "danger", "✕"));
        assert_eq!(row.receipt.artifact_id, "art-42");
        assert_eq!(row.receipt.kind, "DECISION");
        let plain = [Message::from_json(serde_json::json!({"id": "m2", "role": "user", "text": "The user chose: accepted."}).as_object().unwrap())];
        let rows = clarp_core::presentation::present(&plain, &mut clarp_core::presentation::Settings::default(), None).rows;
        assert_eq!(super::message_row(&rows[0], false, &Default::default()).receipt.key, "", "the user's own words stay a message");
    }

    /// A reply that was only spoken markup shows nothing, not its tags.
    #[test]
    fn a_reply_of_voice_markup_only_shows_no_tags() {
        let messages = [Message::from_json(serde_json::json!({"id": "a1", "role": "assistant", "text": "<speak><vox>um</vox> <break time=\"350ms\"/></speak>", "tools": [{"name": "Read"}]}).as_object().unwrap())];
        let rows = clarp_core::presentation::present(&messages, &mut clarp_core::presentation::Settings::default(), None).rows;
        let row = super::message_row(&rows[0], true, &Default::default());
        assert_eq!(slint::Model::row_count(&row.blocks), 0, "the raw text is never the fallback");
    }

}
