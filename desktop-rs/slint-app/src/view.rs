//! Engine state as Slint rows: chats, messages, tool cards, attachments,
//! and the reading theme's palette.

use clarp_core::presentation::PresentedRow;
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

thread_local! {
    /// Installed font families, once `fc-list` has answered.
    static FONTS: std::cell::RefCell<Option<std::collections::HashSet<String>>> = const { std::cell::RefCell::new(None) };
}

/// Reads the installed families off the UI thread, then applies the theme
/// again with the family it can really use.
pub(crate) fn load_fonts(theme: String) {
    let spawned = std::thread::Builder::new().name("font-list".into()).spawn(move || {
        let listed = std::process::Command::new("fc-list").args([":", "family"]).output();
        let families: std::collections::HashSet<String> = match listed {
            Ok(output) => String::from_utf8_lossy(&output.stdout)
                .lines()
                .flat_map(|line| line.split(',').map(|f| f.trim().to_owned()).collect::<Vec<_>>())
                .filter(|f| !f.is_empty())
                .collect(),
            Err(error) => {
                eprintln!("clarp-slint: cannot list fonts: {error}");
                return;
            }
        };
        let applied = slint::invoke_from_event_loop(move || {
            FONTS.with(|fonts| *fonts.borrow_mut() = Some(families));
            if let Some(window) = crate::window() {
                let current = crate::app().map(|app| app.engine.borrow().reading_theme()).unwrap_or(theme);
                apply_theme(&window, &current);
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

/// The theme's first installed family (its first choice until the font
/// list is known).
pub(crate) fn theme_font(theme: &clarp_core::json::Object) -> String {
    FONTS.with(|fonts| match fonts.borrow().as_ref() {
        Some(installed) => clarp_core::reading_theme::resolve_font(theme, |family| installed.contains(family)),
        None => clarp_core::reading_theme::resolve_font(theme, |_| true),
    })
}

pub(crate) fn apply_theme(window: &AppWindow, id: &str) {
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
    palette.set_body_family(theme_font(theme).into());
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

pub(crate) fn chat_row(row: &clarp_core::roster::AgentRow, depth: usize, selected: &str) -> ChatRow {
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
        jobs: row.background_job_count,
        sub_agents: row.sub_agent_count,
        children: row.running_children,
        ..ChatRow::default()
    }
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
/// A shown row's source: the presented row, whether it is open, and its
/// artifacts' ids and revisions.
pub(crate) type Shown = (PresentedRow, bool, String);

pub(crate) fn sync_rows(model: &VecModel<MessageRow>, shown: &mut Vec<Shown>, fresh: Vec<Shown>, rows: Vec<MessageRow>) {
    use slint::Model;
    let id = |row: &Shown| row.0.message.id.clone();
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
    let text = if row.body.is_empty() && !row.activity { m.text.clone() } else { row.body.clone() };
    // The user's own words stay literal; replies are Markdown.
    let blocks = if author == "user" {
        vec![clarp_engine::blocks::Block::Prose(text.clone())]
    } else {
        clarp_engine::blocks::blocks(&text)
    };
    let from_agent = author == "user" && m.origin == "agent" && !m.sender_name.is_empty();
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
            let mut shown: Vec<crate::MessageBlock> = blocks.iter().flat_map(|b| crate::artifacts_view::with_images(b, author == "user")).collect();
            // Image blocks the keyboard reaches (J/K) and enlarges: not the
            // web's, which are never fetched.
            for (index, block) in shown.iter_mut().enumerate() {
                if block.kind == "images" && slint::Model::iter(&block.images).any(|i| !i.foreign) {
                    block.key = crate::artifacts_view::image_key(&m.id, index).into();
                }
            }
            shown
        })),
        sender: if from_agent { m.sender_name.clone() } else { String::new() }.into(),
        stamp: message_stamp(&m.timestamp).into(),
        meta: SharedString::new(),
        pending: m.pending,
        failed: m.delivery_failed,
        activity_label: label.into(),
        group_id: group_id.into(),
        expanded: open,
        tools: ModelRc::new(VecModel::from(
            row.tools.iter().filter(|t| crate::cells_view::shows_tool(row, t)).map(tool_row).collect::<Vec<_>>(),
        )),
        cells: ModelRc::new(VecModel::from(crate::cells_view::cells(row))),
        artifacts: ModelRc::new(VecModel::<crate::ArtifactItem>::default()),
    }
}


#[cfg(test)]
mod tests {
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
}
