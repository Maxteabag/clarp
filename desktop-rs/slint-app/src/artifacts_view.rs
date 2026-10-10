//! Artifact cards in the chat (iOS `ArtifactCard`): each type's inline
//! fields, the clock countdowns tick on, and what the cards' actions do.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::Value;
use slint::ComponentHandle;

use clarp_engine::{Change, Engine};
use slint::Model;

use crate::{App, AppWindow, ArtifactBridge, ArtifactItem, ArtifactOption, CardHint};

fn text(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}

fn number(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0)
}

/// The epoch second `ArtifactBridge.now` counts from: the cards' clock is
/// small integers, which Slint holds exactly.
fn epoch() -> i64 {
    static START: OnceLock<i64> = OnceLock::new();
    *START.get_or_init(|| chrono::Utc::now().timestamp())
}

/// Seconds on the cards' clock now.
pub fn clock_now() -> i32 {
    (chrono::Utc::now().timestamp() - epoch()) as i32
}

/// iOS `CountdownDisplay`: before the target the time remaining, for a
/// minute after it "Now", then the time since.
pub fn countdown(target: i64, now: i64) -> (&'static str, String) {
    let delta = target - now;
    let clock = |seconds: i64| {
        let (days, rest) = (seconds / 86_400, seconds % 86_400);
        let hms = format!("{:02}:{:02}:{:02}", rest / 3600, rest % 3600 / 60, rest % 60);
        if days > 0 { format!("{days}d {hms}") } else { hms }
    };
    if delta > 0 {
        ("Remaining", clock(delta))
    } else if delta > -60 {
        ("Target reached", "Now".to_owned())
    } else {
        ("Since target", clock(-delta))
    }
}

/// The status worth a badge (iOS shows draft, failed, cancelled, expired).
fn badge(status: &str) -> String {
    match status {
        "draft" | "failed" | "cancelled" | "expired" => {
            let mut chars = status.chars();
            chars.next().map(|c| c.to_uppercase().collect::<String>() + chars.as_str()).unwrap_or_default()
        }
        _ => String::new(),
    }
}

/// The card's fields for one artifact (the Host's flat-v1 shape).
pub fn artifact_item(artifact: &Value) -> ArtifactItem {
    let kind = if text(artifact, "type").is_empty() { "item".to_owned() } else { text(artifact, "type") };
    let status = text(artifact, "status");
    let outcome = [text(artifact, "conclusion"), status.clone()].into_iter().find(|s| !s.is_empty()).unwrap_or_else(|| "unknown".into());
    let failed = matches!(outcome.as_str(), "failed" | "failure" | "timed_out" | "action_required");
    let plan = artifact.get("plan").cloned().unwrap_or(Value::Null);
    let (total, completed) = if kind == "plan" {
        (number(&plan, "total_count"), number(&plan, "completed_count"))
    } else {
        (number(artifact, "total_steps"), number(artifact, "completed_steps"))
    };
    let progress = if !matches!(kind.as_str(), "plan" | "workflow_run") {
        String::new()
    } else if total > 0 {
        format!("{} / {} completed", completed.max(0), total)
    } else if outcome == "active" {
        "In progress".into()
    } else {
        String::new()
    };
    // iOS names only a file card for its file.
    let named = if kind == "file" { text(artifact, "file_name") } else { String::new() };
    let title = [named, text(artifact, "title"), text(artifact, "file_name")].into_iter().find(|s| !s.is_empty()).unwrap_or_else(|| "Artifact".into());
    let mut item = ArtifactItem {
        id: text(artifact, "artifact_id").into(),
        label: kind.replace('_', " ").to_uppercase().into(),
        kind: kind.clone().into(),
        outcome: outcome.into(),
        failed,
        badge: badge(&status).into(),
        title: title.into(),
        summary: text(artifact, "summary").into(),
        progress: progress.into(),
        form: kind == "html_form",
        ..ArtifactItem::default()
    };
    match kind.as_str() {
        "countdown" => {
            countdown_fields(&mut item, artifact, &status);
            set_note(&mut item, &text(artifact, "content"));
        }
        "decision" | "question" => decision_fields(&mut item, artifact),
        "plan" => plan_fields(&mut item, artifact),
        "directory" => {
            // iOS: the relative path; it opens in the file manager.
            let relative = text(artifact, "relative_path");
            item.repo = relative.clone().into();
            if relative_ok(&relative) && matches!(text(artifact, "root").as_str(), "workspace" | "home") {
                item.action = "Open folder".into();
            } else {
                item.file_info = "Folder unavailable".into();
            }
        }
        "workflow_run" => {
            // iOS: the step under way and completed/total, a bar when it
            // counts, an indeterminate one while active.
            let (total, done) = (number(artifact, "total_steps"), number(artifact, "completed_steps"));
            item.label = "WORKFLOW RUN".into();
            item.current = text(artifact, "current_step").into();
            item.repo = [text(artifact, "workflow_name"), format!("run {}", text(artifact, "run_id"))].into_iter().filter(|p| !p.is_empty() && p != "run ").collect::<Vec<_>>().join(" · ").into();
            if total > 0 {
                item.progress_value = done.clamp(0, total) as f32 / total as f32;
                item.progress_count = format!("{done}/{total}").into();
            } else if status == "active" {
                item.progress_value = -2.0;
            } else {
                item.progress_value = -1.0;
            }
            if text(artifact, "run_url").starts_with("https://github.com/") {
                item.action = "Open in GitHub".into();
            }
        }
        "release" => {
            // iOS's operation body: version ?? commit ?? "Unknown revision".
            item.revision = [text(artifact, "version"), text(artifact, "commit")].into_iter().find(|r| !r.is_empty()).unwrap_or_else(|| "Unknown revision".into()).into();
            item.release_state = match status.as_str() {
                "ready" | "completed" => "ready",
                "failed" => "failed",
                "active" => "active",
                _ => "other",
            }
            .into();
            item.action = "Open release".into();
        }
        "file" => {
            // iOS's detail: the kind (from the type or the name) and size.
            let name = text(artifact, "file_name");
            let mime = text(artifact, "mime_type");
            let ext = std::path::Path::new(&name).extension().map(|e| e.to_string_lossy().to_uppercase()).unwrap_or_default();
            let sub = mime.rsplit('/').next().unwrap_or_default().to_uppercase();
            let kind = if !ext.is_empty() && ext.len() <= 5 { ext } else if !sub.is_empty() { sub } else { "File".into() };
            let size = number(artifact, "size_bytes");
            let size = if size >= 1 << 20 {
                format!("{:.1} MB", size as f64 / f64::from(1 << 20))
            } else if size > 0 {
                format!("{:.1} KB", size as f64 / 1024.0)
            } else {
                String::new()
            };
            let info = [kind, size].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" · ");
            if host_path(&text(artifact, "url")).is_some() {
                item.file_info = info.into();
                item.action = "Open file".into();
            } else {
                item.file_info = format!("File unavailable · {info}").into();
            }
        }
        "video" => {
            let length = number(artifact, "duration_ms") / 1000;
            if length > 0 {
                item.media_length = format!("{}:{:02}", length / 60, length % 60).into();
            }
            if host_path(&text(artifact, "url")).is_some() {
                item.action = "Play video".into();
            } else {
                item.media_text = "Video unavailable".into();
            }
        }
        "audio" => {
            item.media_state = "idle".into();
            item.media_started = -1;
            let length = number(artifact, "duration_ms") / 1000;
            if length > 0 {
                item.media_length = format!("{}:{:02}", length / 60, length % 60).into();
                item.media_seconds = length as i32;
            }
            if host_path(&text(artifact, "url")).is_some() {
                item.action = "Play".into();
            } else {
                item.media_text = "Audio unavailable".into();
            }
        }
        "data" => data_fields(&mut item, artifact),
        "code_change" => {
            // iOS: the repository (or "Repository"), the branch, the files
            // and lines changed.
            let repository = text(artifact, "repository");
            let branch = text(artifact, "branch");
            let repo = if repository.is_empty() { "Repository".to_owned() } else { repository };
            item.repo = if branch.is_empty() { repo } else { format!("{repo} · {branch}") }.into();
            if artifact.get("files_changed").is_some() {
                let files = number(artifact, "files_changed");
                item.files = format!("{files} file{}", if files == 1 { "" } else { "s" }).into();
            }
            if artifact.get("additions").is_some() {
                item.additions = format!("+{}", number(artifact, "additions")).into();
            }
            if artifact.get("deletions").is_some() {
                item.deletions = format!("−{}", number(artifact, "deletions")).into();
            }
            if !text(artifact, "diff").is_empty() || text(artifact, "source_url").starts_with("https://") {
                item.action = "Open change".into();
            }
        }
        "document" | "research" => {
            let content = text(artifact, "content");
            if !content.trim().is_empty() {
                // HTML reads as its text, Markdown without its marks.
                let markdown = if clarp_core::text::looks_like_html_report(&content) { crate::updates_view::html_markdown(&content) } else { content };
                item.preview = preview_text(&markdown).chars().take(600).collect::<String>().into();
                item.action = if kind == "document" { "Open document" } else { "Open research" }.into();
            }
            let sources = https_sources(artifact).len();
            if sources > 0 {
                item.sources = format!("{sources} source{}", if sources == 1 { "" } else { "s" }).into();
            }
        }
        "html_form" => {
            let report = is_report(artifact);
            item.label = if report { "REPORT" } else { "FORM" }.into();
            item.action = if report { "Open report" } else { "Open form" }.into();
            item.form = false;
        }
        _ => {}
    }
    item
}

/// The Markdown body the card shows under its summary.
fn set_note(item: &mut ArtifactItem, markdown: &str) {
    let markdown = markdown.trim();
    item.note = markdown.into();
    item.note_styled = crate::view::styled(markdown, false);
}

/// What the window holds for the cards beyond the artifacts: the card the
/// keyboard is on and the answers chosen on cards.
pub struct CardState<'a> {
    pub cursor: &'a str,
    pub choices: &'a std::collections::HashMap<String, i32>,
    /// Answers of one's own as typed, and the card whose field is open.
    pub drafts: &'a std::collections::HashMap<String, String>,
    pub editing: &'a str,
    /// Decisions this window has shown pending.
    pub seen_pending: &'a std::collections::HashSet<String>,
}

thread_local! {
    /// Posters fetched for video cards, by artifact.
    static POSTERS: std::cell::RefCell<std::collections::HashMap<String, slint::Image>> = std::cell::RefCell::default();
    /// Fetches in flight: (artifact, purpose), and for "open" the file name.
    static FETCHING: std::cell::RefCell<std::collections::HashMap<(String, String), String>> = std::cell::RefCell::default();
}

/// Before a chat's cards are drawn: asks for posters not yet fetched, and
/// takes what has arrived (posters decoded, files saved and opened).
pub fn settle_downloads(app: &App, session: &str) {
    let artifacts = app.engine.borrow().artifacts_for_session(session);
    for artifact in &artifacts {
        let id = text(artifact, "artifact_id");
        let poster = text(artifact, "thumbnail_url");
        let key = (id.clone(), "poster".to_owned());
        let wanted = POSTERS.with(|p| !p.borrow().contains_key(&id)) && !FETCHING.with(|f| f.borrow().contains_key(&key));
        if text(artifact, "type") == "video" && host_path(&poster).is_some() && wanted {
            FETCHING.with(|f| f.borrow_mut().insert(key, String::new()));
            app.engine.borrow_mut().fetch_artifact_bytes(&id, "poster", &poster);
        }
    }
    let pending: Vec<((String, String), String)> = FETCHING.with(|f| f.borrow().iter().map(|(k, v)| (k.clone(), v.clone())).collect());
    for ((id, purpose), name) in pending {
        let Some(result) = app.engine.borrow_mut().take_artifact_bytes(&id, &purpose) else { continue };
        FETCHING.with(|f| f.borrow_mut().remove(&(id.clone(), purpose.clone())));
        match (purpose.as_str(), result) {
            ("image", Ok(bytes)) => decode_picture_later(id, bytes),
            ("image", Err(error)) => {
                eprintln!("clarp-slint: no image at {id}: {error}");
                PICTURES.with(|p| p.borrow_mut().insert(id.clone(), Picture::Failed));
                PICTURES_LANDED.with(|l| l.set(l.get() + 1));
            }
            ("poster", Ok(bytes)) => match image::load_from_memory(&bytes) {
                Ok(decoded) => {
                    let rgba = decoded.to_rgba8();
                    let buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), rgba.width(), rgba.height());
                    POSTERS.with(|p| p.borrow_mut().insert(id, slint::Image::from_rgba8(buffer)));
                }
                Err(error) => eprintln!("clarp-slint: the poster for {id} is not an image: {error}"),
            },
            ("poster", Err(error)) => eprintln!("clarp-slint: no poster for {id}: {error}"),
            (_, Ok(bytes)) => {
                let said = match save_and_open(&id, &name, &bytes) {
                    Ok(()) => if purpose == "video" { "Opened in your video player" } else { "Opened" }.to_owned(),
                    Err(error) => format!("Couldn't open it: {error}"),
                };
                app.engine.borrow_mut().set_artifact_status(&id, &said);
            }
            (_, Err(error)) => app.engine.borrow_mut().set_artifact_status(&id, &format!("Couldn't download it: {error}")),
        }
    }
}

/// Saves a downloaded artifact under the cache (its own folder, its file's
/// own name) and opens it with the desktop's app for it; checks record it
/// to `CLARP_TEST_OPEN_URL` instead.
fn save_and_open(id: &str, name: &str, bytes: &[u8]) -> Result<(), String> {
    let root = clarp_core::media::cache_dir().ok_or("no cache folder")?;
    let safe = |s: &str| s.chars().map(|c| if c.is_alphanumeric() || matches!(c, '.' | '-' | '_' | ' ') { c } else { '_' }).collect::<String>();
    let file = std::path::Path::new(name).file_name().map(|n| safe(&n.to_string_lossy())).filter(|n| !n.is_empty() && n != "." && n != "..").unwrap_or_else(|| "artifact".into());
    let folder = root.join("artifacts").join(safe(id));
    std::fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
    let path = folder.join(file);
    std::fs::write(&path, bytes).map_err(|e| e.to_string())?;
    if let Some(record) = std::env::var_os("CLARP_TEST_OPEN_URL") {
        use std::io::Write;
        let mut log = std::fs::OpenOptions::new().create(true).append(true).open(record).map_err(|e| e.to_string())?;
        return writeln!(log, "file://{}", path.display()).map_err(|e| e.to_string());
    }
    std::process::Command::new(crate::platform::OPENER).arg(&path).spawn().map(|_| ()).map_err(|e| e.to_string())
}

/// Downloads an artifact's file from the Host to open it.
fn download(app: &App, artifact: &Value, purpose: &str) {
    let id = text(artifact, "artifact_id");
    let url = text(artifact, "url");
    let Some(path) = host_path(&url) else {
        eprintln!("clarp-slint: not downloading {url}: only the Host's media is fetched");
        return;
    };
    let key = (id.clone(), purpose.to_owned());
    if FETCHING.with(|f| f.borrow().contains_key(&key)) {
        return;
    }
    let name = [text(artifact, "file_name"), text(artifact, "title")].into_iter().find(|n| !n.is_empty()).unwrap_or_default();
    FETCHING.with(|f| f.borrow_mut().insert(key, name));
    app.engine.borrow_mut().set_artifact_status(&id, "Downloading…");
    app.engine.borrow_mut().fetch_artifact_bytes(&id, purpose, path);
}

/// A directory's path stays under its root: relative, no "..".
fn relative_ok(path: &str) -> bool {
    !path.is_empty() && !path.starts_with('/') && !path.starts_with('~') && std::path::Path::new(path).components().all(|c| matches!(c, std::path::Component::Normal(_)))
}

/// Opens a directory artifact's folder in the file manager when this
/// desktop shares the Host's files (iOS opens its own file explorer at
/// "@root/path"); else the card says where it is on the Host.
fn open_directory(app: &App, artifact: &Value) {
    let id = text(artifact, "artifact_id");
    let relative = text(artifact, "relative_path");
    if !relative_ok(&relative) {
        return;
    }
    let session = text(artifact, "session");
    let base = match text(artifact, "root").as_str() {
        "home" => std::env::var_os("HOME").map(std::path::PathBuf::from),
        _ => app.engine.borrow().roster().find(&session).map(|a| std::path::PathBuf::from(&a.working_directory)).filter(|p| !p.as_os_str().is_empty()),
    };
    let shared = app.engine.borrow().shared_filesystem();
    // Resolved, it must still be under its root: a link inside the folder
    // does not lead out of it.
    let root = base.as_ref().and_then(|b| std::fs::canonicalize(b).ok());
    let local = base
        .as_ref()
        .map(|b| b.join(&relative))
        .filter(|_| shared)
        .and_then(|p| std::fs::canonicalize(p).ok())
        .filter(|p| p.is_dir() && root.as_ref().is_some_and(|r| p.starts_with(r)));
    let Some(folder) = local else {
        let root = if text(artifact, "root") == "home" { "~" } else { "the agent's folder" };
        app.engine.borrow_mut().set_artifact_status(&id, &format!("On the Host: {root}/{relative}"));
        return;
    };
    match url::Url::from_file_path(&folder) {
        Ok(url) => {
            crate::profile_view::open_file_url(url.as_str());
            app.engine.borrow_mut().set_artifact_status(&id, "");
        }
        Err(()) => eprintln!("clarp-slint: cannot open {}", folder.display()),
    }
}

// ---- images in messages

/// How a message's image is going, by its Host path.
#[derive(Clone)]
enum Picture {
    Loading,
    Loaded(slint::Image),
    Failed,
}

thread_local! {
    static PICTURES: std::cell::RefCell<std::collections::HashMap<String, Picture>> = std::cell::RefCell::default();
    /// Bumped whenever a picture lands, so rows with images are drawn again.
    static PICTURES_LANDED: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// The longest side a chat picture is kept at, in pixels. A 4K screenshot
/// decoded whole is 33 MB, and making it took three copies on the UI
/// thread; the window never shows one larger than this.
const PICTURE_SIDE: u32 = 2048;

/// Decodes a chat picture off the UI thread; it lands in `picture_landed`.
fn decode_picture_later(id: String, bytes: Vec<u8>) {
    crate::platform::runtime::handle().spawn_blocking(move || {
        let pixels = picture_pixels(&bytes);
        drop(bytes);
        if let Err(error) = slint::invoke_from_event_loop(move || picture_landed(id, pixels)) {
            eprintln!("clarp-slint: dropped a decoded picture: {error}");
        }
    });
}

/// A picture's pixels, at most `PICTURE_SIDE` on its longest side, made
/// with one copy beyond the decode.
fn picture_pixels(bytes: &[u8]) -> Result<slint::SharedPixelBuffer<slint::Rgba8Pixel>, String> {
    let decoded = image::load_from_memory(bytes).map_err(|e| e.to_string())?;
    let decoded = if decoded.width().max(decoded.height()) > PICTURE_SIDE { decoded.thumbnail(PICTURE_SIDE, PICTURE_SIDE) } else { decoded };
    let rgba = decoded.into_rgba8();
    Ok(slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), rgba.width(), rgba.height()))
}

fn picture_landed(id: String, pixels: Result<slint::SharedPixelBuffer<slint::Rgba8Pixel>, String>) {
    let picture = match pixels {
        Ok(pixels) => Picture::Loaded(slint::Image::from_rgba8(pixels)),
        Err(error) => {
            eprintln!("clarp-slint: {id} is not an image: {error}");
            Picture::Failed
        }
    };
    PICTURES.with(|p| p.borrow_mut().insert(id, picture));
    PICTURES_LANDED.with(|l| l.set(l.get() + 1));
    if let Some(app) = crate::app() {
        app.refresh(&[Change::Updates]);
    }
}

/// iOS `mediaRequest`: clarp-media://asset/<id> and /media/... (or
/// media/...) are the Host's; anything else is not fetched.
fn media_path(reference: &str) -> Option<String> {
    if let Some(asset) = reference.strip_prefix("clarp-media://asset/") {
        let ok = !asset.is_empty() && asset.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'));
        return ok.then(|| format!("/media/{asset}"));
    }
    if reference.starts_with("/media/") {
        return Some(reference.to_owned());
    }
    reference.starts_with("media/").then(|| format!("/{reference}"))
}

/// The `![alt](url)` references a paragraph consists of, or None when it
/// holds anything else.
fn image_lines(paragraph: &str) -> Option<Vec<(String, String)>> {
    let mut found = Vec::new();
    for line in paragraph.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let rest = line.strip_prefix("![")?;
        let (alt, rest) = rest.split_once("](")?;
        let url = rest.strip_suffix(')')?;
        if url.contains(char::is_whitespace) {
            return None;
        }
        found.push((alt.to_owned(), url.to_owned()));
    }
    (!found.is_empty()).then_some(found)
}

/// A message's image (fetched once from the Host, with the app's token).
fn message_image(alt: &str, reference: &str) -> crate::MessageImage {
    let mut image = crate::MessageImage { url: reference.into(), alt: alt.into(), ..crate::MessageImage::default() };
    let Some(path) = media_path(reference) else {
        image.failed = true;
        image.foreign = true;
        return image;
    };
    let state = PICTURES.with(|p| p.borrow().get(&path).cloned());
    match state {
        Some(Picture::Loaded(picture)) => {
            image.image = picture;
            image.loaded = true;
        }
        Some(Picture::Failed) => image.failed = true,
        Some(Picture::Loading) => {}
        None => {
            PICTURES.with(|p| p.borrow_mut().insert(path.clone(), Picture::Loading));
            FETCHING.with(|f| f.borrow_mut().insert((path.clone(), "image".into()), String::new()));
            if let Some(app) = crate::app() {
                app.engine.borrow_mut().fetch_artifact_bytes(&path, "image", &path);
            }
        }
    }
    image
}

fn images_block(images: Vec<crate::MessageImage>, gallery: bool) -> crate::MessageBlock {
    crate::MessageBlock { kind: "images".into(), images: slint::ModelRc::new(slint::VecModel::from(images)), gallery, ..crate::MessageBlock::default() }
}

/// A message block as the transcript draws it: a paragraph of image
/// references becomes an image (a single one) or a gallery, and a
/// clarp-gallery fence a gallery (iOS's MarkdownParser).
pub fn with_images(block: &clarp_engine::blocks::Block, literal: bool) -> Vec<crate::MessageBlock> {
    use clarp_engine::blocks::Block;
    match block {
        Block::Code { language, text } if !literal && matches!(language.as_str(), "clarp-gallery" | "gallery") => {
            match image_lines(text) {
                Some(found) => vec![images_block(found.iter().map(|(a, u)| message_image(a, u)).collect(), true)],
                None => vec![crate::view::message_block(block, literal)],
            }
        }
        Block::Prose(markdown) if !literal && markdown.contains("![") => {
            let mut out = Vec::new();
            let mut prose: Vec<&str> = Vec::new();
            for paragraph in markdown.split("\n\n") {
                match image_lines(paragraph) {
                    Some(found) => {
                        if !prose.is_empty() {
                            out.push(crate::view::message_block(&Block::Prose(prose.join("\n\n")), literal));
                            prose.clear();
                        }
                        let gallery = found.len() > 1;
                        out.push(images_block(found.iter().map(|(a, u)| message_image(a, u)).collect(), gallery));
                    }
                    None => prose.push(paragraph),
                }
            }
            if !prose.is_empty() {
                out.push(crate::view::message_block(&Block::Prose(prose.join("\n\n")), literal));
            }
            out
        }
        _ => vec![crate::view::message_block(block, literal)],
    }
}

/// What a row's images add to its signature (they redraw when one lands).
pub fn pictures_landed() -> u64 {
    PICTURES_LANDED.with(std::cell::Cell::get)
}

/// A Host-relative media path (`/media/<asset>`); iOS also plays https
/// links, but the app only fetches from its Host, with its token.
fn host_path(url: &str) -> Option<&str> {
    (url.starts_with('/') && !url.starts_with("//")).then_some(url)
}

/// The card's playback as the audio has it.
fn media_fields(item: &mut ArtifactItem) {
    let Some(media) = crate::platform::audio::with(|audio| audio.media().cloned()).flatten() else { return };
    if media.artifact != item.id.as_str() {
        return;
    }
    use crate::platform::audio::MediaState;
    let (state, said, action) = match &media.state {
        MediaState::Preparing => ("preparing", "Preparing audio…".to_owned(), "Play"),
        MediaState::Playing => ("playing", "Playing".to_owned(), "Pause"),
        MediaState::Paused => ("paused", "Paused".to_owned(), "Play"),
        MediaState::Failed(why) => ("failed", why.clone(), "Play"),
    };
    item.media_state = state.into();
    item.media_text = said.into();
    item.action = action.into();
    item.media_played = media.played.as_secs() as i32;
    // The clock's second this stretch began.
    item.media_started = match media.since {
        Some(since) => clock_now() - since.elapsed().as_secs() as i32,
        None => -1,
    };
}

/// What the cards last showed of playback, to redraw them when it moves.
fn media_snapshot() -> String {
    crate::platform::audio::with(|audio| audio.media().map(|m| format!("{}{:?}{}", m.artifact, m.state, m.played.as_millis()))).flatten().unwrap_or_default()
}

thread_local! {
    static MEDIA_SHOWN: std::cell::RefCell<String> = const { std::cell::RefCell::new(String::new()) };
}

/// The audio changed: the cards follow when their playback did.
pub fn media_changed(app: &App) {
    let now = media_snapshot();
    if MEDIA_SHOWN.with(|m| *m.borrow() != now) {
        MEDIA_SHOWN.with(|m| *m.borrow_mut() = now);
        app.refresh(&[Change::Updates]);
    }
}

/// A card as the chat shows it: its fields, whether the keyboard is on it,
/// what it chose and how its latest action went.
pub fn card(artifact: &Value, engine: &Engine, state: &CardState) -> ArtifactItem {
    let mut item = artifact_item(artifact);
    item.selected = !state.cursor.is_empty() && item.id == state.cursor;
    item.status_text = engine.artifact_status(&item.id).into();
    if item.kind == "audio" && !item.action.is_empty() {
        media_fields(&mut item);
    }
    if let Some(poster) = POSTERS.with(|p| p.borrow().get(item.id.as_str()).cloned()) {
        item.poster = poster;
        item.has_poster = true;
    }
    if item.pending {
        let choices = item.options.row_count() + usize::from(item.allow_custom);
        item.chosen = state.choices.get(item.id.as_str()).copied().filter(|c| (*c as usize) < choices).unwrap_or(-1);
        item.draft = state.drafts.get(item.id.as_str()).cloned().unwrap_or_default().into();
        item.editing = item.allow_custom && state.editing == item.id.as_str();
    }
    // Answered in this window, it keeps the line it had, so its height.
    item.keep_line = (item.pending && !item.meta.is_empty()) || !item.delivery.is_empty() || state.seen_pending.contains(item.id.as_str());
    item.hints = slint::ModelRc::new(slint::VecModel::from(card_hints(&item, &crate::commands::overrides_of(engine))));
    item
}

/// A card key as the user bound it (the default spelled as is, without
/// resolving the map, while they have not).
fn card_key(action: &str, overrides: &crate::keymap::Overrides) -> String {
    let default = match action {
        "artifact-open" => "O",
        "artifact-discard" => "Del",
        "artifact-back" => "Left",
        "artifact-forward" => "Right",
        "artifact-stop" => "S",
        _ => "",
    };
    crate::keymap::shown_or("pane", action, overrides, default)
}

/// A card's keys, as its hints show them (and the shortcut bar while the
/// keyboard is on it): what each does, and the action a click runs.
fn card_hints(item: &ArtifactItem, overrides: &crate::keymap::Overrides) -> Vec<CardHint> {
    let hint = |key: &str, label: &str, action: &str| CardHint { key: key.into(), label: label.into(), action: action.into(), chosen: false };
    let key = |action: &str| card_key(action, overrides);
    let mut hints = Vec::new();
    if matches!(item.kind.as_str(), "decision" | "question") {
        if !item.pending {
            return hints;
        }
        let answers = item.options.row_count() + usize::from(item.allow_custom);
        if item.approval {
            for (index, option) in item.options.iter().enumerate() {
                hints.push(CardHint { chosen: item.chosen == index as i32, ..hint(&(index + 1).to_string(), &option.label, &format!("answer:{index}")) });
            }
        }
        // The chosen answer's digit again sends it (Enter is the
        // composer's); a written answer's field sends on its own Enter.
        // Before a choice Send is a button with no key (a click says to
        // choose first).
        if item.editing {
            hints.push(hint("Enter", "Send", "send"));
        } else if (0..answers as i32).contains(&item.chosen) {
            hints.push(hint(&format!("{} again", item.chosen + 1), "Send", "send"));
        } else if answers > 0 {
            hints.push(hint("", "Send", "send"));
        }
        if item.editing {
            hints.push(hint("Esc", "Keep draft", "keep-draft"));
        } else {
            hints.push(hint(&key("artifact-discard"), "Discard", "discard"));
        }
        return hints;
    }
    if item.action.is_empty() {
        return hints;
    }
    hints.push(hint(&key("artifact-open"), &item.action, "open"));
    if item.kind == "audio" {
        hints.push(hint(&format!("{}/{}", key("artifact-back"), key("artifact-forward")).replace("Left/Right", "←/→"), "Seek", "seek"));
        hints.push(hint(&key("artifact-stop"), "Stop", "stop"));
    }
    hints
}

/// Brings a reply's card model to `cards`, in place when the same cards
/// are there (only the ones that changed are set).
pub fn update_cards(model: &slint::VecModel<ArtifactItem>, cards: Vec<ArtifactItem>) {
    let same = model.row_count() == cards.len() && cards.iter().enumerate().all(|(i, c)| model.row_data(i).is_some_and(|m| m.id == c.id));
    if !same {
        model.set_vec(cards);
        return;
    }
    for (index, card) in cards.into_iter().enumerate() {
        if model.row_data(index).as_ref() != Some(&card) {
            model.set_row_data(index, card);
        }
    }
}

/// iOS `DecisionResponseView`: while pending the question, why and how
/// urgent, and its answers; once answered, how and with what.
fn decision_fields(item: &mut ArtifactItem, artifact: &Value) {
    let Some(decision) = artifact.get("decision").filter(|d| d.is_object()) else { return };
    let status = text(decision, "status");
    item.question = text(decision, "question").into();
    item.context = text(decision, "context").into();
    let response = text(decision, "response_type");
    item.approval = response.is_empty() || response == "approval";
    let options: Vec<ArtifactOption> = if item.approval {
        let label = |key: &str, fallback: &str| Some(text(decision, key)).filter(|l| !l.is_empty()).unwrap_or_else(|| fallback.to_owned());
        vec![
            ArtifactOption { id: "yes".into(), label: label("yes_label", "Yes").into(), ..ArtifactOption::default() },
            ArtifactOption { id: "no".into(), label: label("no_label", "No").into(), ..ArtifactOption::default() },
        ]
    } else {
        let recommended = text(decision, "recommended_option_id");
        decision
            .get("options")
            .and_then(Value::as_array)
            .map(|options| {
                options
                    .iter()
                    .map(|o| {
                        let (id, label) = (text(o, "id"), text(o, "label"));
                        let recommended = !recommended.is_empty() && id == recommended && !label.to_lowercase().contains("recommended");
                        ArtifactOption { id: id.into(), label: label.into(), detail: text(o, "description").into(), recommended }
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    item.pending = status == "pending";
    item.chosen = -1;
    item.allow_custom = !item.approval && decision.get("allow_custom_text").and_then(Value::as_bool) == Some(true);
    // A question keeps its options once answered, the answer marked, so
    // its card keeps its height.
    if response == "single_choice" {
        item.options = slint::ModelRc::new(slint::VecModel::from(options.clone()));
    }
    if item.pending {
        let mut meta = Vec::new();
        if !text(decision, "priority_reason").is_empty() {
            meta.push(text(decision, "priority_reason"));
        }
        match text(decision, "response_effort").as_str() {
            "quick" | "" => {}
            "short" => meta.push("About a minute".into()),
            _ => meta.push("Needs a closer look".into()),
        }
        let deadline = chrono::DateTime::parse_from_rfc3339(&text(decision, "deadline_at"));
        if let Ok(deadline) = deadline {
            meta.insert(0, format!("Due {}", deadline.with_timezone(&chrono::Local).format("%b %-d at %H:%M")));
        } else if text(decision, "urgency") == "time_sensitive" {
            meta.push("Time sensitive".into());
        }
        if !item.approval && response != "single_choice" {
            meta.push("Update Clarp to answer this kind of question".into());
        }
        item.meta = meta.join(" · ").into();
        if item.approval {
            item.options = slint::ModelRc::new(slint::VecModel::from(options));
        }
        return;
    }
    let (resolved, ok) = match status.as_str() {
        "answered" => ("Answer saved", true),
        "accepted" => ("Approved", true),
        "rejected" => ("Declined", false),
        // Host contract 28: why it was cancelled (iOS's labels).
        "cancelled" => match text(decision, "resolved_choice").as_str() {
            "withdrawn" => ("Withdrawn by agent", false),
            "superseded" => ("Closed by your reply", false),
            _ => ("Discarded", false),
        },
        "expired" => ("Expired", false),
        _ => ("", false),
    };
    item.resolved = resolved.into();
    item.resolved_ok = ok;
    let answer = decision.get("answer").cloned().unwrap_or(Value::Null);
    let chosen = text(&answer, "option_id");
    item.answer = [text(&answer, "text"), text(&answer, "label")]
        .into_iter()
        .find(|t| !t.is_empty())
        .or_else(|| options.iter().find(|o| !chosen.is_empty() && o.id == chosen.as_str()).map(|o| o.label.to_string()))
        .unwrap_or_default()
        .into();
    if !text(&answer, "text").is_empty() {
        item.chosen = options.len() as i32;
        item.draft = text(&answer, "text").into();
    } else if let Some(index) = options.iter().position(|o| !chosen.is_empty() && o.id == chosen.as_str()) {
        item.chosen = index as i32;
    }
    if decision.get("delivery_pending").and_then(Value::as_bool) == Some(true) {
        item.delivery = "Saved. Waiting to deliver to the agent.".into();
    }
}

/// 1-9 on a card: the answer the keyboard chose.
pub fn choose(app: &App, id: &str, index: i32) {
    let Some(card) = card_item(app, id).filter(|c| c.pending) else { return };
    let options = card.options.row_count();
    if index < 0 || index as usize > options || (index as usize == options && !card.allow_custom) {
        return;
    }
    app.artifact_choices.borrow_mut().insert(id.to_owned(), index);
    // The last number is an answer of one's own: its field takes the keyboard.
    *app.artifact_editing.borrow_mut() = if index as usize == options { id.to_owned() } else { String::new() };
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    app.refresh(&[Change::Updates]);
}

/// The card as the open chat shows it.
pub fn card_item(app: &App, id: &str) -> Option<ArtifactItem> {
    app.active_messages()?.iter().flat_map(|row| row.artifacts.iter().collect::<Vec<_>>()).find(|a| a.id == id)
}

/// Sends a decision's chosen answer.
pub fn send(app: &App, id: &str) {
    let Some(artifact) = artifact(app, id) else { return };
    let Some(card) = card_item(app, id).filter(|c| c.pending) else { return };
    let decision = artifact.get("decision").cloned().unwrap_or(Value::Null);
    let own = card.allow_custom && card.chosen == card.options.row_count() as i32;
    if own {
        // iOS caps an answer of one's own at 4,000 characters.
        let written: String = app.artifact_drafts.borrow().get(id).map(|d| d.trim().chars().take(4000).collect()).unwrap_or_default();
        if written.is_empty() {
            app.engine.borrow_mut().set_artifact_status(id, "Choose an answer first: write one");
            app.refresh(&[Change::Updates]);
            return;
        }
        app.artifact_editing.borrow_mut().clear();
        // Sent: the keyboard is back on the chat.
        app.focus_transcript();
        app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "resolve", serde_json::json!({"answer": {"text": written}}), number(&decision, "revision"));
        pump();
        return;
    }
    let Some(option) = usize::try_from(card.chosen).ok().and_then(|i| card.options.row_data(i)) else {
        app.engine.borrow_mut().set_artifact_status(id, "Choose an answer first (1-9)");
        app.refresh(&[Change::Updates]);
        return;
    };
    let body = if card.approval {
        serde_json::json!({"choice": if option.id == "yes" { "accepted" } else { "rejected" }})
    } else {
        serde_json::json!({"answer": {"option_id": option.id.as_str()}})
    };
    app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "resolve", body, number(&decision, "revision"));
    pump();
}

/// Discards a pending decision (iOS "Discard").
pub fn discard(app: &App, id: &str) {
    let Some(artifact) = artifact(app, id) else { return };
    if !card_item(app, id).is_some_and(|c| c.pending) {
        return;
    }
    let decision = artifact.get("decision").cloned().unwrap_or(Value::Null);
    app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "dismiss", serde_json::json!({}), number(&decision, "revision"));
    pump();
}

/// Shows the engine's change at once.
fn pump() {
    if let Some(app) = crate::app() {
        crate::pump_now(&app);
    }
}

fn artifact(app: &App, id: &str) -> Option<Value> {
    let found = app.engine.borrow().update_artifacts().iter().find(|a| text(a, "artifact_id") == id).cloned();
    if found.is_none() {
        eprintln!("clarp-slint: no artifact {id}");
    }
    found
}

/// A body's first lines as plain text: its blocks (paragraphs, headings,
/// lists) kept apart by " · ", code left out.
fn preview_text(markdown: &str) -> String {
    let mut blocks: Vec<String> = Vec::new();
    let (mut current, mut fenced) = (Vec::new(), false);
    for line in markdown.lines() {
        if line.trim_start().starts_with("```") {
            fenced = !fenced;
            continue;
        }
        if fenced {
            continue;
        }
        // A heading or a list item starts a block of its own.
        let starts = line.trim_start().starts_with('#') || line.trim_start().starts_with("- ") || line.trim_start().starts_with("* ");
        if line.trim().is_empty() || starts {
            blocks.push(current.join("\n"));
            current.clear();
        }
        if !line.trim().is_empty() {
            current.push(line);
        }
    }
    blocks.push(current.join("\n"));
    blocks.iter().map(|b| clarp_core::text::plain_preview_text(b)).filter(|b| !b.is_empty()).collect::<Vec<_>>().join(" · ")
}

/// iOS `ArtifactScalar`: true reads Yes, false No, null —.
fn scalar(value: &Value) -> String {
    match value {
        Value::Null => "—".into(),
        Value::Bool(true) => "Yes".into(),
        Value::Bool(false) => "No".into(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// A data artifact's columns and rows (as text), when it has columns.
fn table(artifact: &Value) -> Option<(Vec<String>, Vec<Vec<String>>)> {
    let columns: Vec<String> = artifact.get("columns")?.as_array()?.iter().map(scalar).collect();
    if columns.is_empty() {
        return None;
    }
    let rows = artifact
        .get("rows")
        .and_then(Value::as_array)
        .map(|rows| rows.iter().map(|r| r.as_array().map(|cells| cells.iter().map(scalar).collect()).unwrap_or_default()).collect())
        .unwrap_or_default();
    Some((columns, rows))
}

/// At most seven cells: six and what is left.
fn clipped(cells: &[String], rest: impl Fn(usize) -> String) -> Vec<slint::SharedString> {
    if cells.len() <= 7 {
        return cells.iter().map(|c| c.as_str().into()).collect();
    }
    cells[..6].iter().map(|c| c.as_str().into()).chain(std::iter::once(rest(cells.len() - 6).into())).collect()
}

/// iOS's data card: the header and the first row, and how many rows.
fn data_fields(item: &mut ArtifactItem, artifact: &Value) {
    let Some((columns, rows)) = table(artifact) else {
        item.data_note = "Structured data unavailable".into();
        return;
    };
    let model = |cells: Vec<slint::SharedString>| slint::ModelRc::new(slint::VecModel::from(cells));
    item.head = model(clipped(&columns, |more| format!("+{more}")));
    if let Some(first) = rows.first() {
        item.first_row = model(clipped(first, |_| "…".into()));
    }
    item.rows_count = format!("{} row{}", rows.len(), if rows.len() == 1 { "" } else { "s" }).into();
    item.action = "Open table".into();
}

/// A Markdown table of `rows` under `head`.
fn markdown_table(head: &[String], rows: &[Vec<String>]) -> String {
    let cell = |t: &str| t.replace('|', "\\|").replace('\n', " ");
    let line = |cells: &[String]| format!("| {} |", cells.iter().map(|c| cell(c)).collect::<Vec<_>>().join(" | "));
    let mut lines = vec![line(head), format!("|{}|", vec!["---"; head.len()].join("|"))];
    lines.extend(rows.iter().map(|r| line(r)));
    lines.join("\n")
}

/// A plan's items and subtasks, depth first.
fn plan_items(plan: &Value) -> Vec<(usize, Value)> {
    fn walk(items: &Value, depth: usize, out: &mut Vec<(usize, Value)>) {
        for item in items.as_array().into_iter().flatten() {
            out.push((depth, item.clone()));
            walk(item.get("subtasks").unwrap_or(&Value::Null), depth + 1, out);
        }
    }
    let mut out = Vec::new();
    walk(plan.get("items").unwrap_or(&Value::Null), 0, &mut out);
    out
}

/// iOS's plan card: items and subtasks completed against the plan's
/// total, and the first one under way.
fn plan_fields(item: &mut ArtifactItem, artifact: &Value) {
    let Some(plan) = artifact.get("plan").filter(|p| p.is_object()) else {
        item.progress_value = -1.0;
        item.current = "Plan details unavailable".into();
        return;
    };
    let items = plan_items(plan);
    let done = items.iter().filter(|(_, i)| text(i, "status") == "completed").count() as i64;
    let total = match number(plan, "total_count") {
        0 => items.len() as i64,
        total => total,
    };
    item.progress_value = done as f32 / total.max(1) as f32;
    item.progress_count = format!("{done}/{total}").into();
    item.current = items.iter().find(|(_, i)| text(i, "status") == "in_progress").map(|(_, i)| text(i, "title")).unwrap_or_default().into();
    // Nothing under way but a step blocked: that step, marked.
    if item.current.is_empty() {
        if let Some((_, blocked)) = items.iter().find(|(_, i)| matches!(text(i, "status").as_str(), "blocked" | "failed")) {
            item.current = text(blocked, "title").into();
            item.current_blocked = true;
        }
    }
    item.action = "Open plan".into();
}

/// A research artifact's sources that open: titled https links (iOS shows
/// no others).
fn https_sources(artifact: &Value) -> Vec<(String, String)> {
    artifact
        .get("sources")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|s| (text(s, "title"), text(s, "url")))
        .filter(|(_, url)| url.starts_with("https://"))
        .map(|(title, url)| (if title.is_empty() { url.clone() } else { title }, url))
        .collect()
}

/// The report viewer's model for an artifact without a report body:
/// what its card stands for, in full.
pub fn detail(app: &App, id: &str) -> Option<clarp_core::json::Object> {
    let artifact = app.engine.borrow().update_artifacts().iter().find(|a| text(a, "artifact_id") == id).cloned()?;
    let card = artifact_item(&artifact);
    // Whether the body came from HTML (already read as Markdown here).
    let mut from_html = false;
    let (summary, body) = match text(&artifact, "type").as_str() {
        "plan" => {
            let plan = artifact.get("plan").filter(|p| p.is_object())?;
            let summary = [text(plan, "title"), text(plan, "goal"), format!("{} done", card.progress_count)]
                .into_iter()
                .filter(|p| !p.is_empty())
                .collect::<Vec<_>>()
                .join(" · ");
            // A table: one row per item and subtask, the state beside it.
            let cell = |t: String| t.replace('|', "\\|");
            let mut rows = vec!["| Step | State |".to_owned(), "|---|---|".to_owned()];
            for (depth, i) in plan_items(plan) {
                let (mark, state) = match text(&i, "status").as_str() {
                    "completed" => ("✓", "done"),
                    "in_progress" => ("▶", "under way"),
                    "blocked" | "failed" => ("✕", "blocked"),
                    _ => ("○", "to do"),
                };
                let detail = text(&i, "detail");
                let indent = "↳ ".repeat(depth);
                let state = if detail.is_empty() { state.to_owned() } else { format!("{state} · *{}*", cell(detail)) };
                rows.push(format!("| {indent}{mark} {} | {state} |", cell(text(&i, "title"))));
            }
            let lines = rows;
            (summary, lines.join("\n"))
        }
        "research" => {
            // The body as the Host's report (HTML sanitized, read as
            // Markdown), then the sources as links.
            let report = app.engine.borrow().report_for_artifact(id).unwrap_or_default();
            let body = clarp_core::json::string(&report, "body");
            from_html = clarp_core::json::boolean(&report, "isHtml");
            let mut markdown = if from_html { crate::updates_view::html_markdown(&body) } else { body };
            let sources = https_sources(&artifact);
            if !sources.is_empty() {
                let escape = |t: &str| t.replace('[', "\\[").replace(']', "\\]");
                let list: Vec<String> = sources.iter().map(|(title, url)| format!("- [{}]({url})", escape(title))).collect();
                markdown = format!("{markdown}\n\n## Sources\n\n{}", list.join("\n"));
            }
            (text(&artifact, "summary"), markdown)
        }
        "release" => {
            let facts: Vec<Vec<String>> = [("Version", "version"), ("Build", "build"), ("Revision", "commit"), ("Repository", "repository")]
                .iter()
                .filter_map(|(label, key)| Some(text(&artifact, key)).filter(|v| !v.is_empty()).map(|v| vec![(*label).to_owned(), v]))
                .collect();
            let mut parts = Vec::new();
            if !facts.is_empty() {
                parts.push(markdown_table(&["Release".to_owned(), card.title.to_string()], &facts));
            }
            let notes = text(&artifact, "content");
            if !notes.trim().is_empty() {
                parts.push(notes);
            }
            let source = text(&artifact, "source_url");
            if source.starts_with("https://") {
                parts.push(format!("[Open source]({source})"));
            }
            let build = text(&artifact, "build");
            (if build.is_empty() { card.revision.to_string() } else { format!("{} · build {build}", card.revision) }, parts.join("\n\n"))
        }
        "code_change" => {
            let commit = text(&artifact, "commit");
            let summary = if commit.is_empty() { card.repo.to_string() } else { format!("{} · {commit}", card.repo) };
            let mut parts = Vec::new();
            let stats = [card.files.to_string(), card.additions.to_string(), card.deletions.to_string()].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>();
            if !stats.is_empty() {
                parts.push(stats.join(" · "));
            }
            let diff = text(&artifact, "diff");
            if !diff.is_empty() {
                parts.push(format!("```diff\n{}\n```", diff.trim_end()));
            }
            let source = text(&artifact, "source_url");
            if source.starts_with("https://") {
                parts.push(format!("[Open source]({source})"));
            }
            (summary, parts.join("\n\n"))
        }
        "data" => {
            let (columns, rows) = table(&artifact)?;
            let mut parts = Vec::new();
            // The chart as bars: the first 30 categories, scaled to the largest.
            let chart = artifact.get("chart").cloned().unwrap_or(Value::Null);
            let at = |key: &str, fallback: usize| columns.iter().position(|c| *c == text(&chart, key)).unwrap_or(fallback);
            if matches!(text(&chart, "kind").as_str(), "bar" | "line") {
                let (category, value) = (at("category_column", 0), at("value_column", 1));
                let points: Vec<(String, f64)> = rows.iter().take(30).filter_map(|r| Some((r.get(category)?.clone(), r.get(value)?.parse::<f64>().ok()?))).collect();
                let top = points.iter().map(|(_, v)| v.abs()).fold(0.0, f64::max).max(f64::MIN_POSITIVE);
                let bars: Vec<Vec<String>> =
                    points.iter().map(|(c, v)| vec![c.clone(), format!("{} {v}", "━".repeat(((v.abs() / top) * 24.0).round() as usize))]).collect();
                if !bars.is_empty() {
                    parts.push(markdown_table(&[columns[category].clone(), columns.get(value).cloned().unwrap_or_default()], &bars));
                }
            }
            // The grid, as iOS caps it: 500 rows.
            parts.push(markdown_table(&columns, &rows[..rows.len().min(500)]));
            (format!("{} · {} columns", card.rows_count, columns.len()), parts.join("\n\n"))
        }
        _ => return None,
    };
    serde_json::json!({"artifact_id": id, "title": card.title.as_str(), "summary": summary, "type": card.kind.as_str(), "isHtml": from_html, "converted": true, "kind": card.label.as_str(), "body": body})
        .as_object()
        .cloned()
}

/// iOS `isHTMLReport`: read-only, or an answer schema that asks nothing.
fn is_report(artifact: &Value) -> bool {
    let flagged = |v: &Value| v.get("read_only").and_then(Value::as_bool) == Some(true);
    if flagged(artifact) || artifact.get("payload").is_some_and(flagged) {
        return true;
    }
    let schema = artifact.get("answer_schema").cloned().unwrap_or(Value::Null);
    let fields = schema.get("properties").and_then(Value::as_object).map(|p| p.len());
    fields == Some(0) || (fields.is_none() && schema.get("additionalProperties") == Some(&Value::Bool(false)))
}

/// Where a card was when it last reported.
struct Report {
    shown: bool,
    x: f32,
    top: f32,
    width: f32,
    height: f32,
    view_top: f32,
    view_bottom: f32,
    /// The report numbers of its last three reports, newest first.
    seqs: [u64; 3],
}

impl Report {
    fn shown_in(top: f32, height: f32, view_top: f32, view_bottom: f32) -> bool {
        top + height > view_top + 4.0 && top < view_bottom - 4.0
    }
}

thread_local! {
    static SHOWN: std::cell::RefCell<(u64, std::collections::HashMap<String, Report>)> = std::cell::RefCell::default();
}

/// A drawn card reports where it is every 150 ms. A row the list dropped
/// stops: it has lapsed once another card has reported three times since
/// (counted in reports, not time, so a busy moment lapses nothing).
fn card_shown(id: &str, x: f32, top: f32, width: f32, height: f32, view_top: f32, view_bottom: f32) {
    let shown = Report::shown_in(top, height, view_top, view_bottom);
    SHOWN.with(|s| {
        let (seq, reports) = &mut *s.borrow_mut();
        *seq += 1;
        let report = reports.entry(id.to_owned()).or_insert(Report { shown, x, top, width, height, view_top, view_bottom, seqs: [0; 3] });
        *report = Report { shown, x, top, width, height, view_top, view_bottom, seqs: [*seq, report.seqs[0], report.seqs[1]] };
    });
}

/// The heights the cards on screen last reported (for the checks).
pub fn card_heights(app: &App) -> Vec<(String, f32)> {
    let ids = on_screen(app);
    SHOWN.with(|s| ids.into_iter().filter_map(|id| s.borrow().1.get(&id).map(|r| r.height).map(|h| (id, h))).collect())
}

/// Where a card on screen last was: its left, top, width and height (for
/// the checks, which click its hints).
pub fn card_rect(app: &App, id: &str) -> Option<(f32, f32, f32, f32)> {
    on_screen(app).contains(&id.to_owned()).then(|| SHOWN.with(|s| s.borrow().1.get(id).map(|r| (r.x, r.top, r.width, r.height)))).flatten()
}

/// The open chat's cards now on screen, top to bottom.
pub fn on_screen(app: &App) -> Vec<String> {
    SHOWN.with(|s| {
        let s = s.borrow();
        let reports = &s.1;
        let lapsed = |report: &Report| reports.values().any(|other| other.seqs[2] > report.seqs[0]);
        selectables(app).into_iter().filter(|id| reports.get(id).is_some_and(|r| r.shown && !lapsed(r))).collect()
    })
}

/// What J/K reach in the open chat, top to bottom: each message's image
/// blocks (a single image or a gallery) and then the cards made while it
/// was written.
pub fn selectables(app: &App) -> Vec<String> {
    let Some(rows) = app.active_messages() else { return Vec::new() };
    let mut ids = Vec::new();
    for row in rows.iter() {
        // A resolved decision's receipt.
        if !row.receipt.key.is_empty() {
            ids.push(row.receipt.key.to_string());
        }
        ids.extend(row.blocks.iter().filter(|b| !b.key.is_empty()).map(|b| b.key.to_string()));
        ids.extend(row.artifacts.iter().map(|a| a.id.to_string()));
        // A live item row that opens (a tool, a group, the fold).
        if row.live.expandable && !row.live.key.is_empty() {
            ids.push(row.live.key.to_string());
        }
        // Another agent's prompt, folded or open.
        if !row.prompt.key.is_empty() {
            ids.push(row.prompt.key.to_string());
        }
    }
    ids
}

/// An image block's key: its message and its place among the blocks.
pub fn image_key(row: &str, index: usize) -> String {
    format!("img:{row}:{index}")
}

/// The pictures of the image block `key` in the open chat.
fn image_block(app: &App, key: &str) -> Option<Vec<crate::MessageImage>> {
    let (row, index) = key.strip_prefix("img:")?.rsplit_once(':')?;
    let index: usize = index.parse().ok()?;
    let rows = app.active_messages()?;
    let found = rows.iter().find(|r| r.id == row)?;
    let block = found.blocks.row_data(index)?;
    Some(block.images.iter().collect())
}

thread_local! {
    /// The keyboard's tile in a gallery.
    static TILE: std::cell::Cell<i32> = const { std::cell::Cell::new(0) };
    /// A viewer opened from the chat's keyboard: closing it goes back there.
    static RETURN_TO_CHAT: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

pub fn tile() -> i32 {
    TILE.with(std::cell::Cell::get)
}

/// Whether the dialog closing was opened from a card (and so the chat, on
/// that card, gets the keyboard back); asking clears it.
pub fn take_return() -> bool {
    RETURN_TO_CHAT.with(|r| r.replace(false))
}

/// A live item row or another agent's prompt: O (or a click) opens and
/// folds it.
fn toggles(id: &str) -> bool {
    id.starts_with("live:") || id.starts_with(clarp_core::agent_prompt::PREFIX)
}

/// O on a live item row or a prompt (or a click): opens or folds it, kept
/// by its key.
pub fn toggle_live(app: &App, key: &str) {
    *app.artifact_cursor.borrow_mut() = key.to_owned();
    {
        let mut expanded = app.expanded.borrow_mut();
        if !expanded.remove(key) {
            expanded.insert(key.to_owned());
        }
    }
    let session = app.active_session();
    app.refresh(&[Change::Live(session), Change::Updates]);
}

/// The receipt `key` in the open chat.
fn receipt(app: &App, key: &str) -> Option<crate::ReceiptRow> {
    app.active_messages()?.iter().find(|r| r.receipt.key == key).map(|r| r.receipt)
}

/// A receipt whose decision card is in the chat (a link hint reaches it).
pub fn receipt_linked(app: &App, key: &str) -> bool {
    key.starts_with("receipt:") && receipt(app, key).is_some_and(|r| r.linked)
}

/// O on a receipt (or a click): the keyboard goes to the decision card it
/// answers, brought into view.
pub fn show_decision(app: &App, key: &str) {
    let Some(receipt) = receipt(app, key) else {
        eprintln!("clarp-slint: no receipt {key}");
        return;
    };
    let card = receipt.artifact_id.to_string();
    if !receipt.linked || !selectables(app).contains(&card) {
        eprintln!("clarp-slint: the decision card {card} is not in the chat");
        return;
    }
    let direction = if selectables(app).iter().position(|i| *i == card) < selectables(app).iter().position(|i| i == key) { -1 } else { 1 };
    TILE.with(|t| t.set(0));
    *app.artifact_cursor.borrow_mut() = card.clone();
    bring_into_view(app, &card, direction);
    app.refresh(&[Change::Updates]);
}

fn live_row(app: &App, key: &str) -> Option<crate::MessageRow> {
    app.active_messages()?.iter().find(|r| r.live.key == key)
}

/// O on what the keyboard is on (or its link hint's number): a card's
/// action, or an image enlarged.
pub fn activate(app: &App, window: &AppWindow, id: &str) {
    if id.starts_with("receipt:") {
        show_decision(app, id);
        return;
    }
    if toggles(id) {
        toggle_live(app, id);
        return;
    }
    if id.starts_with("img:") {
        open_image(app, window, id, tile());
    } else {
        open(app, window, id);
    }
}

/// Enlarges the image block `key` at picture `index`.
pub fn open_image(app: &App, window: &AppWindow, key: &str, index: i32) {
    let Some(images) = image_block(app, key) else {
        eprintln!("clarp-slint: no image block {key}");
        return;
    };
    let index = index.clamp(0, images.len().saturating_sub(1) as i32);
    *app.artifact_cursor.borrow_mut() = key.to_owned();
    TILE.with(|t| t.set(index));
    window.set_image_view_count(images.len() as i32);
    window.set_image_view_index(index);
    window.set_image_view_images(slint::ModelRc::new(slint::VecModel::from(images)));
    crate::commands::open_overlay(app, window, "image");
    RETURN_TO_CHAT.with(|r| r.set(true));
    window.invoke_focus_image_view();
    app.refresh(&[Change::Updates]);
}

/// Left/Right in the enlarged image: the gallery's next or previous picture
/// (the gallery's tile follows).
pub fn image_view_moved(app: &App, window: &AppWindow, delta: i32) {
    let index = (window.get_image_view_index() + delta).clamp(0, (window.get_image_view_count() - 1).max(0));
    window.set_image_view_index(index);
    TILE.with(|t| t.set(index));
    window.global::<ArtifactBridge>().set_tile(index);
    let _ = app;
}

/// Left/Right on the keyboard's card: a gallery's tiles, an audio clip's
/// position (10 s); false when the card has neither.
pub fn nudge(app: &App, window: &AppWindow, direction: i32) -> bool {
    let Some(id) = selected(app) else { return false };
    if id.starts_with("img:") {
        let count = image_block(app, &id).map_or(0, |i| i.len() as i32);
        if count < 2 {
            return false;
        }
        let next = (tile() + direction).clamp(0, count - 1);
        TILE.with(|t| t.set(next));
        window.global::<ArtifactBridge>().set_tile(next);
        return true;
    }
    let Some(card) = card_item(app, &id).filter(|c| c.kind == "audio" && !c.action.is_empty()) else { return false };
    let length = (card.media_seconds > 0).then(|| Duration::from_secs(card.media_seconds as u64));
    crate::platform::audio::with(|audio| audio.seek_media(&id, 10 * i64::from(direction), length));
    true
}

/// S on an audio card: stops its clip.
pub fn stop(app: &App) -> bool {
    let Some(id) = selected(app).filter(|id| card_item(app, id).is_some_and(|c| c.kind == "audio")) else { return false };
    crate::platform::audio::with(|audio| audio.stop_media(&id));
    true
}

/// A click on a card's hint: what its key does, on that card.
fn hint_clicked(app: &App, window: &AppWindow, id: &str, action: &str) {
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    match action {
        "open" => open(app, window, id),
        "send" => send(app, id),
        "discard" => discard(app, id),
        "keep-draft" => app.focus_transcript(),
        "seek" => {
            nudge(app, window, 1);
        }
        "stop" => {
            stop(app);
        }
        _ => match action.strip_prefix("answer:").and_then(|i| i.parse::<i32>().ok()) {
            Some(index) => {
                choose(app, id, index);
                send(app, id);
            }
            None => eprintln!("clarp-slint: no hint action {action}"),
        },
    }
    app.refresh(&[Change::Updates]);
    crate::commands::show_hints(app, window);
}

/// The keys of what the keyboard is on, for the shortcut bar.
pub fn selected_hints(app: &App) -> Option<Vec<(String, String)>> {
    let id = selected(app)?;
    // The keys as the user bound them.
    let overrides = crate::commands::overrides(app);
    let key = |action: &str| card_key(action, &overrides);
    if id.starts_with("live:") {
        let open = live_row(app, &id).is_some_and(|r| r.live.expanded);
        return Some(vec![(key("artifact-open"), if open { "Collapse" } else { "Expand" }.to_owned())]);
    }
    if id.starts_with("receipt:") {
        return Some(if receipt(app, &id).is_some_and(|r| r.linked) { vec![(key("artifact-open"), "Show decision".to_owned())] } else { Vec::new() });
    }
    if id.starts_with(clarp_core::agent_prompt::PREFIX) {
        let open = app.active_messages()?.iter().any(|r| r.prompt.key == id && r.prompt.expanded);
        return Some(vec![(key("artifact-open"), if open { "Collapse" } else { "Expand" }.to_owned())]);
    }
    if id.starts_with("img:") {
        let gallery = image_block(app, &id).is_some_and(|i| i.len() > 1);
        let mut hints = vec![(key("artifact-open"), "Enlarge".to_owned())];
        if gallery {
            hints.push((format!("{}/{}", key("artifact-back"), key("artifact-forward")).replace("Left/Right", "←/→"), "Tile".into()));
        }
        return Some(hints);
    }
    let card = card_item(app, &id)?;
    // A button with no key is no key hint.
    Some(card.hints.iter().filter(|h| !h.key.is_empty()).map(|h| (h.key.to_string(), h.label.to_string())).collect())
}

pub fn has_cards(app: &App) -> bool {
    !selectables(app).is_empty()
}

/// The card the keyboard is on, while it is on screen in the open chat.
pub fn selected(app: &App) -> Option<String> {
    let cursor = app.artifact_cursor.borrow().clone();
    (!cursor.is_empty() && on_screen(app).contains(&cursor)).then_some(cursor)
}

/// J/K: the next or previous card in the chat, scrolled into view when it
/// is not (all) on screen. Only a card on screen is stepped from (a cursor
/// left on a card the reader scrolled away from is not), and there is no
/// step past the first or last card. From none, J is the topmost card on
/// screen and K the lowest; with none on screen nothing happens, so K at
/// the latest message never sends the reader to a card far up. Only these
/// keypresses move the reader to a card.
pub fn step(app: &App, direction: i32) {
    let ids = selectables(app);
    let cursor = app.artifact_cursor.borrow().clone();
    let at = selected(app).and_then(|selected| ids.iter().position(|i| *i == selected));
    let shown = on_screen(app);
    let next = match at {
        Some(index) => match index.checked_add_signed(direction as isize).filter(|next| *next < ids.len()) {
            Some(next) => next,
            None => return,
        },
        None => {
            let Some(pick) = (if direction > 0 { shown.first() } else { shown.last() }) else { return };
            let Some(index) = ids.iter().position(|i| i == pick) else { return };
            index
        }
    };
    if ids[next] != cursor {
        TILE.with(|t| t.set(0));
    }
    *app.artifact_cursor.borrow_mut() = ids[next].clone();
    bring_into_view(app, &ids[next], direction);
    app.refresh(&[Change::Updates]);
}

/// Bringing the keyboard's card into view: which, which way it lies, the
/// report number of the last scroll (only reports after it count), and
/// how often it has been placed. The list estimates the rows it has not
/// drawn and corrects them as they draw, so a scroll can land elsewhere
/// than asked: the card is placed until its own report says it is in view.
struct Seek {
    id: String,
    direction: i32,
    since_seq: u64,
    scrolled: std::time::Instant,
    started: std::time::Instant,
    placements: u32,
    /// The card's last report (number and top): the list settles for a
    /// while after a scroll, so a place counts once it holds still.
    seen: Option<(u64, f32)>,
    /// How often its row was asked into view by the measured heights.
    rows_asked: u32,
}

thread_local! {
    /// When the last seek ended: its card's own report may still be on
    /// its way, so the selection is kept a moment longer.
    static SEEK_ENDED: std::cell::Cell<Option<std::time::Instant>> = const { std::cell::Cell::new(None) };
    static SEEK: std::cell::RefCell<Option<Seek>> = const { std::cell::RefCell::new(None) };
    static SEEK_TIMER: std::cell::RefCell<Option<slint::Timer>> = const { std::cell::RefCell::new(None) };
    /// The report number and time of the last scroll a seek made: reports
    /// from before it say where cards were, not where they are.
    static LAST_SCROLL: std::cell::Cell<(u64, Option<std::time::Instant>)> = const { std::cell::Cell::new((0, None)) };
}

/// Whether J/K are still bringing a card into view (or just did).
pub fn seeking() -> bool {
    SEEK.with(|s| s.borrow().is_some()) || SEEK_ENDED.with(|e| e.get().is_some_and(|at| at.elapsed() < Duration::from_millis(500)))
}

/// Scrolls the chat so `id` is wholly in view: at once when it is drawn,
/// else a page at a time towards it (`direction`) until it is.
fn bring_into_view(app: &App, id: &str, direction: i32) {
    let now = std::time::Instant::now();
    let (since_seq, scrolled) = LAST_SCROLL.with(std::cell::Cell::get);
    let scrolled = scrolled.unwrap_or(now - Duration::from_secs(1));
    SEEK.with(|s| *s.borrow_mut() = Some(Seek { id: id.to_owned(), direction, since_seq, scrolled, started: now, placements: 0, seen: None, rows_asked: 0 }));
    // A card on screen or near it is placed now; one further away is found
    // over the next frames.
    if seek_tick(app) {
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::Repeated, Duration::from_millis(30), || {
            let Some(app) = crate::app() else { return };
            if !seek_tick(&app) {
                SEEK_TIMER.with(|t| t.borrow_mut().take());
                if let Some(window) = crate::window() {
                    crate::commands::show_hints(&app, &window);
                }
                app.refresh(&[Change::Updates]);
            }
        });
        SEEK_TIMER.with(|t| *t.borrow_mut() = Some(timer));
    } else {
        SEEK_TIMER.with(|t| t.borrow_mut().take());
    }
}

/// Scrolls by `delta` for the seek, from report number `seq` on.
fn seek_scrolled(app: &App, delta: f32, direction: i32, placed: bool) {
    app.scroll_by(delta);
    let seq = SHOWN.with(|s| s.borrow().0);
    LAST_SCROLL.with(|l| l.set((seq, Some(std::time::Instant::now()))));
    SEEK.with(|s| {
        if let Some(seek) = s.borrow_mut().as_mut() {
            seek.since_seq = seq;
            seek.scrolled = std::time::Instant::now();
            seek.direction = direction;
            seek.placements += u32::from(placed);
            seek.seen = None;
        }
    });
}

/// The row of the open chat that holds `id` (as `selectables` finds it).
fn row_of(app: &App, id: &str) -> Option<usize> {
    let rows = app.active_messages()?;
    rows.iter().position(|row| {
        row.receipt.key == id
            || row.blocks.iter().any(|b| b.key == id)
            || row.artifacts.iter().any(|a| a.id == id)
            || (row.live.expandable && row.live.key == id)
            || row.prompt.key == id
    })
}

/// Asks the transcript to bring `row` into view by the rows' measured
/// heights; from report number `seq` on, the card's reports count.
fn seek_row_asked(app: &App, row: usize, direction: i32) {
    app.seek_row(row, direction > 0);
    let seq = SHOWN.with(|s| s.borrow().0);
    LAST_SCROLL.with(|l| l.set((seq, Some(std::time::Instant::now()))));
    SEEK.with(|s| {
        if let Some(seek) = s.borrow_mut().as_mut() {
            seek.since_seq = seq;
            seek.scrolled = std::time::Instant::now();
            seek.seen = None;
            seek.rows_asked += 1;
        }
    });
}

/// One step of bringing the card into view; false once it is there (or
/// out of reach).
fn seek_tick(app: &App) -> bool {
    const MARGIN: f32 = 12.0;
    let Some((id, mut direction, since_seq, scrolled, started, placements, seen, rows_asked)) =
        SEEK.with(|s| s.borrow().as_ref().map(|k| (k.id.clone(), k.direction, k.since_seq, k.scrolled, k.started, k.placements, k.seen, k.rows_asked)))
    else {
        return false;
    };
    let stop = || {
        SEEK.with(|s| s.borrow_mut().take());
        SEEK_ENDED.with(|e| e.set(Some(std::time::Instant::now())));
        false
    };
    if *app.artifact_cursor.borrow() != id || started.elapsed() > Duration::from_secs(10) {
        return stop();
    }
    // Gone from the chat (its row folded away or replaced): there is no
    // place to bring it to, and paging after it would carry the reader off.
    if !selectables(app).contains(&id) {
        return stop();
    }
    let waited = scrolled.elapsed();
    if since_seq > 0 && waited < Duration::from_millis(160) {
        return true;
    }
    // Its latest report since the last scroll, if any (a dropped row's is
    // stale: it has lapsed).
    let placed = SHOWN.with(|s| {
        let s = s.borrow();
        let lapsed = |report: &Report| s.1.values().any(|other| other.seqs[2] > report.seqs[0]);
        s.1.get(&id).filter(|r| r.seqs[0] > since_seq && !lapsed(r)).map(|r| (r.seqs[0], r.top, r.height, r.view_top, r.view_bottom))
    });
    if let Some((seq, top, height, view_top, view_bottom)) = placed {
        let delta = if top < view_top + MARGIN || height > view_bottom - view_top - 2.0 * MARGIN {
            top - view_top - MARGIN
        } else if top + height > view_bottom - MARGIN {
            top + height - view_bottom + MARGIN
        } else {
            0.0
        };
        // In view (or as near as the chat's ends let it be).
        // In view (or as near as the chat's ends let it be), and holding
        // still there since its last report.
        let still = seen.is_some_and(|(last, at)| last != seq && (at - top).abs() < 1.0);
        if (delta.abs() < 1.0 && still) || placements >= 6 {
            return stop();
        }
        if delta.abs() < 1.0 {
            SEEK.with(|s| {
                if let Some(seek) = s.borrow_mut().as_mut() {
                    seek.seen = Some((seq, top));
                }
            });
            return true;
        }
        seek_scrolled(app, delta, direction, true);
        // Where it now should be, until it reports.
        SHOWN.with(|s| {
            if let Some(report) = s.borrow_mut().1.get_mut(&id) {
                report.top -= delta;
                report.shown = Report::shown_in(report.top, report.height, report.view_top, report.view_bottom);
            }
        });
        return true;
    }
    // Not drawn yet: its row is brought into view by the rows' measured
    // heights (the list's own positions of rows it has not drawn are
    // estimates that shift as it draws them, so paging by them can pass
    // the card back and forth), and then its own report places it. The
    // transcript takes a few frames; the card reports every 150 ms.
    if rows_asked < 3 && (rows_asked == 0 || waited >= Duration::from_millis(700)) {
        if let Some(row) = row_of(app, &id) {
            seek_row_asked(app, row, direction);
            return true;
        }
    }
    if rows_asked > 0 && rows_asked < 3 {
        return true;
    }
    // Else a page towards it, then wait for the rows there to report
    // (each card every 150 ms, once drawn), and page on towards it by the
    // cards that did: past them, or back if it was passed.
    if since_seq > 0 {
        let ids = selectables(app);
        let target = ids.iter().position(|i| *i == id).unwrap_or(0);
        let fresh: Vec<usize> = SHOWN.with(|s| {
            let s = s.borrow();
            ids.iter().enumerate().filter(|(_, i)| s.1.get(*i).is_some_and(|r| r.shown && r.seqs[0] > since_seq)).map(|(index, _)| index).collect()
        });
        match (fresh.first(), fresh.last()) {
            (Some(first), _) if target < *first => direction = -1,
            (_, Some(last)) if target > *last => direction = 1,
            // Among the cards drawn: its own report is on its way.
            (Some(_), Some(_)) if waited < Duration::from_millis(900) => return true,
            (None, None) if waited < Duration::from_millis(400) => return true,
            _ => {}
        }
    }
    let page = SHOWN.with(|s| s.borrow().1.values().map(|r| r.view_bottom - r.view_top).find(|band| *band > 0.0)).unwrap_or(400.0) * 0.8;
    seek_scrolled(app, page * direction as f32, direction, false);
    true
}

/// Escape on a card: the keyboard leaves it (stays on the chat).
pub fn leave(app: &App) {
    app.artifact_cursor.borrow_mut().clear();
    SEEK.with(|s| s.borrow_mut().take());
    app.refresh(&[Change::Updates]);
}

/// A card's action (Enter on the selected card, or a click).
pub fn open(app: &App, window: &AppWindow, id: &str) {
    if id.starts_with("receipt:") {
        show_decision(app, id);
        return;
    }
    if toggles(id) {
        toggle_live(app, id);
        return;
    }
    let Some(artifact) = artifact(app, id) else { return };
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    let network = artifact.get("connect_origins")
        .or_else(|| artifact.get("payload").and_then(|p| p.get("connect_origins")))
        .cloned().unwrap_or(Value::Null);
    // Keep legacy reports in their existing viewer. Only an explicit valid
    // network grant needs the browser's HTML engine; malformed policy denies it.
    let network_report = !crate::form_server::validated_origins(&network).is_empty();
    match text(&artifact, "type").as_str() {
        "html_form" if !is_report(&artifact) || network_report => {
            let interactive = !is_report(&artifact);
            let version = artifact.get("version").cloned().unwrap_or(Value::Null);
            let events = {
                let mut engine = app.engine.borrow_mut();
                if !interactive || !engine.form_events_supported() {
                    None
                } else {
                    match engine.open_form_events(id, &version) {
                        Ok(key) => Some(crate::form_server::Events { store: engine.form_event_store(), key, origin: engine.form_event_origin() }),
                        Err(error) => {
                            eprintln!("clarp-slint: the form's event log is unavailable: {error}");
                            engine.set_artifact_status(id, &format!("Event log: {error}"));
                            None
                        }
                    }
                }
            };
            let owner_origin = app.engine.borrow().form_event_origin();
            match crate::form_server::serve(id, version, &text(&artifact, "content"), events, &network, interactive, &owner_origin) {
                Ok(url) => crate::open_link(&url),
                Err(error) => {
                    eprintln!("clarp-slint: {error}");
                    app.engine.borrow_mut().set_artifact_status(id, &format!("Not opened: {error}"));
                }
            }
        }
        // A decision is answered with its digits: opening it puts the
        // chat's keyboard on it.
        "decision" | "question" => app.focus_transcript(),
        "video" => download(app, &artifact, "video"),
        "file" => download(app, &artifact, "file"),
        "directory" => open_directory(app, &artifact),
        "workflow_run" => crate::open_link(&text(&artifact, "run_url")),
        "audio" => {
            let url = text(&artifact, "url");
            match host_path(&url) {
                Some(path) => {
                    let path = path.to_owned();
                    crate::platform::audio::with(|audio| audio.toggle_media(id, &path));
                }
                None => eprintln!("clarp-slint: not playing {url}: only the Host's media plays"),
            }
        }
        "plan" | "code_change" | "data" | "release" if detail(app, id).is_some() => {
            crate::updates_view::open_report(app, window, id);
            RETURN_TO_CHAT.with(|r| r.set(true));
        }
        _ if app.engine.borrow().report_for_artifact(id).is_some() => {
            crate::updates_view::open_report(app, window, id);
            RETURN_TO_CHAT.with(|r| r.set(true));
        }
        // Nothing to open: Enter only selects.
        _ => {}
    }
    app.refresh(&[Change::Updates]);
}

/// A countdown's target on the cards' clock, and its date in the target's
/// own offset (the Host requires one) with the zone it names.
fn countdown_fields(item: &mut ArtifactItem, artifact: &Value, status: &str) {
    if status == "cancelled" {
        item.countdown_note = "Cancelled".into();
        return;
    }
    let Ok(target) = chrono::DateTime::parse_from_rfc3339(&text(artifact, "target_at")) else {
        item.countdown_note = "Countdown unavailable".into();
        return;
    };
    let zone = text(artifact, "time_zone");
    item.countdown_set = true;
    item.countdown_at = (target.timestamp() - epoch()).clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32;
    item.countdown_line = format!("{} · {}", target.format("%b %-d, %Y at %H:%M"), if zone.is_empty() { "UTC" } else { &zone }).into();
}

/// The bridge's clock and callbacks.
pub fn bind(window: &AppWindow) {
    let bridge = window.global::<ArtifactBridge>();
    bridge.on_countdown_phase(|target, now| countdown(i64::from(target), i64::from(now)).0.into());
    bridge.on_countdown_clock(|target, now| countdown(i64::from(target), i64::from(now)).1.into());
    bridge.on_media_position(|played, started, now| {
        let seconds = played + if started >= 0 { (now - started).max(0) } else { 0 };
        format!("{}:{:02}", seconds / 60, seconds % 60).into()
    });
    bridge.set_now(clock_now());
    bridge.on_link_clicked(|url| crate::open_link(&url));
    bridge.on_hint(|id, action| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            hint_clicked(&app, &window, &id, &action);
        }
    });
    bridge.on_image_clicked(|key, index| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open_image(&app, &window, &key, index);
        }
    });
    bridge.on_reader_moved(|| {
        SEEK.with(|s| s.borrow_mut().take());
    });
    bridge.on_card_shown(|id, x, top, width, height, view_top, view_bottom| card_shown(&id, x, top, width, height, view_top, view_bottom));
    bridge.on_choose(|id, index| {
        if let Some(app) = crate::app() {
            choose(&app, &id, index);
        }
    });
    bridge.on_send(|id| {
        if let Some(app) = crate::app() {
            send(&app, &id);
        }
    });
    bridge.on_draft_edited(|id, text| {
        if let Some(app) = crate::app() {
            app.artifact_drafts.borrow_mut().insert(id.to_string(), text.to_string());
        }
    });
    bridge.on_editing_changed(|id, editing| {
        if let Some(app) = crate::app() {
            if !editing && *app.artifact_editing.borrow() == id.as_str() {
                // Left without sending: the field stays with its draft, but
                // no longer takes the keyboard (the card updates in place).
                app.artifact_editing.borrow_mut().clear();
                app.refresh(&[Change::Updates]);
            }
        }
    });
    bridge.on_discard(|id| {
        if let Some(app) = crate::app() {
            discard(&app, &id);
        }
    });
    bridge.on_open(|id| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open(&app, &window, &id);
        }
    });
    let weak = window.as_weak();
    let timer = slint::Timer::default();
    // A quarter-second check keeps the tick within 250 ms of the second.
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(250), move || {
        if let Some(window) = weak.upgrade() {
            let bridge = window.global::<ArtifactBridge>();
            let now = clock_now();
            if bridge.get_now() != now {
                bridge.set_now(now);
            }
            // A selected card scrolled away is no longer selected.
            if let Some(app) = crate::app() {
                let cursor = app.artifact_cursor.borrow().clone();
                if !cursor.is_empty() && selected(&app).is_none() && app.artifact_editing.borrow().is_empty() && !seeking() {
                    app.artifact_cursor.borrow_mut().clear();
                    app.refresh(&[Change::Updates]);
                    crate::commands::show_hints(&app, &window);
                }
            }
        }
    });
    CLOCK.with(|clock| *clock.borrow_mut() = Some(timer));
}

thread_local! {
    static CLOCK: std::cell::RefCell<Option<slint::Timer>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
mod picture_tests {
    use super::{PICTURE_SIDE, picture_pixels};

    fn png(width: u32, height: u32) -> Vec<u8> {
        let mut bytes = Vec::new();
        image::RgbImage::new(width, height)
            .write_to(&mut std::io::Cursor::new(&mut bytes), image::ImageFormat::Png)
            .expect("encodes");
        bytes
    }

    #[test]
    fn a_large_picture_is_kept_at_most_picture_side_and_a_small_one_whole() {
        let big = picture_pixels(&png(3840, 2160)).expect("decodes");
        assert_eq!((big.width(), big.height()), (PICTURE_SIDE, 1152));
        let small = picture_pixels(&png(400, 240)).expect("decodes");
        assert_eq!((small.width(), small.height()), (400, 240));
        assert!(picture_pixels(b"not an image").is_err());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_countdown_counts_down_then_up() {
        assert_eq!(countdown(100 + 86_400 + 3 * 3600 + 4 * 60 + 5, 100), ("Remaining", "1d 03:04:05".to_owned()));
        assert_eq!(countdown(100, 99), ("Remaining", "00:00:01".to_owned()));
        assert_eq!(countdown(100, 100), ("Target reached", "Now".to_owned()));
        assert_eq!(countdown(100, 159), ("Target reached", "Now".to_owned()));
        assert_eq!(countdown(100, 160), ("Since target", "00:01:00".to_owned()));
    }

    #[test]
    fn a_cancelled_decision_says_why() {
        let cancelled = |choice: &str| {
            let artifact = serde_json::json!({"artifact_id": "d", "type": "decision", "decision": {"status": "cancelled", "resolved_choice": choice}});
            artifact_item(&artifact).resolved.to_string()
        };
        assert_eq!(cancelled("withdrawn"), "Withdrawn by agent");
        assert_eq!(cancelled("superseded"), "Closed by your reply");
        assert_eq!(cancelled(""), "Discarded");
    }

    #[test]
    fn only_unfinished_states_get_a_badge() {
        assert_eq!(badge("cancelled"), "Cancelled");
        assert_eq!(badge("active"), "");
    }
}
