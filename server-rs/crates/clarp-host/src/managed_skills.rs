//! The skills this Host keeps installed for its agents' tools (`GET` and `POST
//! /managed-skills`), and the links it puts in place at its start.
//!
//! The rules are the kernel's (`clarp_kernel::managed_skills`, held to the Python Host); this
//! module finds what is on the disk, asks the kernel, and does what it says. One Host keeps a
//! machine's tools (the lead's ruling of 20 Nov 2026): this one only where it was started with
//! `--accounts select` and a tree of skills (`--skills`). Anywhere else it reads no tool's
//! folder, answers the list empty, refuses every change in one sentence and writes nothing,
//! at a request or at its start.

use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::managed_skills::{self as rules, Act, At, Facts, Link, Malformed, Root};
use clarp_kernel::recovery::Accounts;
use serde_json::{Value, json};

use crate::config::{Config, home};
use crate::edge::{ApiError, App};

/// Where this Host's skills come from and where the tools read theirs, as it was started.
pub struct Shelf {
    /// This release's tree of skills, resolved: what a link leads to.
    tree: PathBuf,
    /// The folders other releases of Clarp live in, resolved: this Host's own, and the
    /// releases of the Host that kept the tools before.
    releases: Vec<String>,
    claude: PathBuf,
    codex: PathBuf,
    agy: PathBuf,
}

impl Shelf {
    /// The shelf of a Host that keeps the machine's tools; none for any other.
    pub fn of(config: &Config) -> Option<Self> {
        if config.accounts != Accounts::Select {
            return None;
        }
        let tree = resolve(config.skills.as_deref()?);
        let mut releases = Vec::new();
        if let Some(own) = tree.parent().and_then(Path::parent) {
            releases.push(text(own));
        }
        if let Some(other) = &config.skills_other_releases {
            releases.push(text(&resolve(other)));
        }
        let var = |name: &str| std::env::var_os(name).map(PathBuf::from);
        let codex_home = var("CODEX_HOME").unwrap_or_else(|| home().join(".codex"));
        Some(Self {
            tree,
            releases,
            claude: var("CLARP_CLAUDE_SKILLS").unwrap_or_else(|| home().join(".claude/skills")),
            codex: var("CLARP_CODEX_SKILLS").unwrap_or_else(|| codex_home.join("skills")),
            agy: var("CLARP_AGY_SKILLS").unwrap_or_else(|| home().join(".gemini/config/skills")),
        })
    }

    fn items(&self) -> Result<Vec<Value>, Malformed> {
        let bytes = std::fs::read(self.tree.join("manifest.json")).ok();
        rules::manifest(bytes.as_deref())
    }

    fn source(&self, skill: &str) -> PathBuf {
        self.tree.join(skill)
    }

    fn has_source(&self, skill: &str) -> bool {
        self.source(skill).join("SKILL.md").is_file()
    }

    fn agy_usable(&self) -> bool {
        let parent_is_dir = self.agy.parent().is_some_and(Path::is_dir);
        rules::agy_usable(parent_is_dir, self.agy.is_symlink(), self.agy.is_dir())
    }

    fn folder(&self, root: Root) -> &Path {
        match root {
            Root::Claude => &self.claude,
            Root::Codex => &self.codex,
            Root::Agy => &self.agy,
        }
    }

    /// The folders read for a row: Claude's and Codex's, and Antigravity's where it is used.
    fn read_roots(&self) -> Vec<Root> {
        let mut roots = vec![Root::Claude, Root::Codex];
        if self.agy_usable() {
            roots.push(Root::Agy);
        }
        roots
    }

    /// The folders a change writes: those read, a folder that is another's written once.
    fn written_roots(&self) -> Vec<Root> {
        let offered: Vec<(Root, String)> = self
            .read_roots()
            .into_iter()
            .map(|root| (root, text(&resolve(self.folder(root)))))
            .collect();
        rules::distinct(&offered)
    }

    fn place(&self, root: Root, skill: &str) -> PathBuf {
        self.folder(root).join(skill)
    }

    fn link(&self, root: Root, skill: &str) -> Link {
        let place = self.place(root, skill);
        let source = text(&resolve(&self.source(skill)));
        let releases: Vec<&str> = self.releases.iter().map(String::as_str).collect();
        let resolved;
        let at = match std::fs::symlink_metadata(&place) {
            Err(_) => At::Nothing,
            Ok(found) if found.file_type().is_symlink() => {
                resolved = text(&resolve(&place));
                At::Link(&resolved)
            }
            Ok(_) => At::Thing,
        };
        rules::link_state(at, &source, &releases, skill)
    }

    /// The rows of `GET /managed-skills`.
    fn status(&self, chosen: &[String]) -> Result<Vec<Value>, Malformed> {
        let roots = self.read_roots();
        let mut rows = Vec::new();
        for item in self.items()? {
            let skill = rules::skill_id(&item)?;
            let links = roots
                .iter()
                .map(|root| (*root, self.link(*root, &skill)))
                .collect();
            let facts = Facts {
                links,
                enabled: chosen.contains(&skill),
                source: self.has_source(&skill),
                requirements: rules::requirements(&item, &mut |name: &str| on_path(name))?,
            };
            rows.push(rules::row(&item, &facts));
        }
        Ok(rows)
    }

    /// Does the acts at a skill's places; on a failure puts back what was there, as the
    /// reference does, and says why.
    fn apply(&self, skill: &str, acts: &[(Root, Act)]) -> Result<Undo, std::io::Error> {
        let mut undo = Undo(Vec::new());
        for (root, act) in acts {
            if *act == Act::Leave {
                continue;
            }
            let place = self.place(*root, skill);
            undo.0
                .push((place.clone(), std::fs::read_link(&place).ok()));
            let done = match act {
                Act::Link => std::fs::create_dir_all(self.folder(*root))
                    .and_then(|()| put_link(&place, &self.source(skill))),
                _ if place.is_symlink() => std::fs::remove_file(&place),
                _ => Ok(()),
            };
            if let Err(error) = done {
                undo.run();
                return Err(error);
            }
        }
        Ok(undo)
    }
}

/// What was at the places a change touched, to put back when the change cannot be kept.
struct Undo(Vec<(PathBuf, Option<PathBuf>)>);

impl Undo {
    fn run(&self) {
        for (place, original) in &self.0 {
            let _ = match original {
                None if place.is_symlink() => std::fs::remove_file(place),
                None => Ok(()),
                Some(target) => place
                    .parent()
                    .map_or(Ok(()), std::fs::create_dir_all)
                    .and_then(|()| put_link(place, target)),
            };
        }
    }
}

/// Makes `place` a link to `target` in one step: a link beside it, renamed over it.
fn put_link(place: &Path, target: &Path) -> std::io::Result<()> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let name = place.file_name().map(text_os).unwrap_or_default();
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let beside = place.with_file_name(format!(".{name}.{}.{n}.next", std::process::id()));
    if beside.is_symlink() {
        std::fs::remove_file(&beside)?;
    }
    std::os::unix::fs::symlink(target, &beside)?;
    std::fs::rename(&beside, place).inspect_err(|_| {
        let _ = std::fs::remove_file(&beside);
    })
}

fn text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn text_os(name: &std::ffi::OsStr) -> String {
    name.to_string_lossy().into_owned()
}

/// A path with every link on it followed as far as it goes, as Python's
/// `Path.resolve(strict=False)` gives it: a part that is not there is kept as written, and a
/// ring of links ends where it is found.
pub fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut todo: Vec<std::ffi::OsString> = parts(&absolute);
    todo.reverse();
    let mut done = PathBuf::from("/");
    let mut hops = 0;
    while let Some(part) = todo.pop() {
        match part.to_str() {
            Some(".") => continue,
            Some("..") => {
                done.pop();
                continue;
            }
            _ => {}
        }
        let next = done.join(&part);
        let target = std::fs::symlink_metadata(&next)
            .ok()
            .filter(|found| found.file_type().is_symlink())
            .and_then(|_| std::fs::read_link(&next).ok());
        match target {
            Some(target) if hops < 40 => {
                hops += 1;
                if target.is_absolute() {
                    done = PathBuf::from("/");
                }
                let mut more = parts(&target);
                more.reverse();
                todo.extend(more);
            }
            Some(_) => {
                let mut ring = next;
                for rest in todo.iter().rev() {
                    ring.push(rest);
                }
                return ring;
            }
            None => done = next,
        }
    }
    done
}

fn parts(path: &Path) -> Vec<std::ffi::OsString> {
    path.components()
        .filter_map(|part| match part {
            Component::Normal(name) => Some(name.to_os_string()),
            Component::CurDir => Some(".".into()),
            Component::ParentDir => Some("..".into()),
            _ => None,
        })
        .collect()
}

/// Whether a program is found as Python's `shutil.which` finds it: a name with a slash is
/// that file, any other is looked for along this Host's `PATH`.
fn on_path(name: &str) -> bool {
    use std::os::unix::fs::PermissionsExt;
    let runs = |path: &Path| {
        std::fs::metadata(path)
            .is_ok_and(|found| !found.is_dir() && found.permissions().mode() & 0o111 != 0)
    };
    if name.contains('/') {
        return runs(Path::new(name));
    }
    let path = std::env::var("PATH").unwrap_or_else(|_| "/bin:/usr/bin".to_owned());
    // An empty part is the directory the Host runs in, as for Python.
    path.split(':').any(|dir| runs(&Path::new(dir).join(name)))
}

/// The chosen list in the store, when the first one has been taken.
fn chosen(app: &App) -> Option<Vec<String>> {
    let kept = app
        .fleet
        .directory()
        .setting(rules::CHOSEN_SETTING)
        .map(str::to_owned)?;
    Some(serde_json::from_str(&kept).unwrap_or_default())
}

async fn keep_chosen(app: &App, chosen: &[String]) -> Result<(), ApiError> {
    let kept = serde_json::to_string(chosen).unwrap_or_else(|_| "[]".to_owned());
    app.fleet
        .set(vec![(rules::CHOSEN_SETTING.to_owned(), kept)])
        .await?;
    Ok(())
}

fn refused(code: &str, why: &str) -> Response {
    let told = json!({ "error": why, "code": code });
    (StatusCode::CONFLICT, Json(told)).into_response()
}

fn unavailable() -> ApiError {
    ApiError::new(StatusCode::SERVICE_UNAVAILABLE, rules::UNAVAILABLE)
}

async fn blocking<T: Send + 'static>(
    work: impl FnOnce() -> T + Send + 'static,
) -> Result<T, ApiError> {
    tokio::task::spawn_blocking(work)
        .await
        .map_err(|error| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, error.to_string()))
}

/// `GET /managed-skills`: the full status where this Host keeps the tools, else none.
async fn list(State(app): State<App>) -> Result<Json<Value>, ApiError> {
    let Some(shelf) = Shelf::of(&app.config) else {
        return Ok(Json(json!({ "skills": [] })));
    };
    let chosen = chosen(&app).unwrap_or_default();
    let rows = blocking(move || shelf.status(&chosen)).await?;
    let rows = rows.map_err(|Malformed| unavailable())?;
    Ok(Json(json!({ "skills": rows })))
}

/// `POST /managed-skills`: a skill switched on or off, where this Host keeps the tools.
async fn change(State(app): State<App>, body: Bytes) -> Response {
    if app.config.accounts != Accounts::Select {
        return refused(rules::KEPT_ELSEWHERE, rules::KEPT_ELSEWHERE_WHY);
    }
    let Some(shelf) = Shelf::of(&app.config) else {
        return refused(rules::NOT_SHIPPED, rules::NOT_SHIPPED_WHY);
    };
    let asked: Option<Value> = if body.is_empty() {
        Some(json!({}))
    } else {
        serde_json::from_slice(&body).ok()
    };
    let (skill, enabled) = match rules::asked(asked.as_ref()) {
        Ok(asked) => asked,
        Err(why) => return ApiError::new(StatusCode::BAD_REQUEST, why).into_response(),
    };
    match changed(&app, shelf, skill, enabled).await {
        Ok(told) => Json(json!({ "ok": true, "skill": told })).into_response(),
        Err(error) => error.into_response(),
    }
}

async fn changed(app: &App, shelf: Shelf, skill: String, enabled: bool) -> Result<Value, ApiError> {
    // One change at a time: each reads the folders and the list it then writes.
    let _one = app.skill_links.lock().await;
    let before = chosen(app).unwrap_or_default();
    let worked = blocking(move || {
        let items = shelf.items()?;
        let item = rules::entry(&items, &skill)?.cloned();
        let roots: Vec<(Root, Link)> = shelf
            .written_roots()
            .into_iter()
            .map(|root| (root, shelf.link(root, &skill)))
            .collect();
        let decided = rules::change(
            item.as_ref(),
            &skill,
            enabled,
            shelf.has_source(&skill),
            &roots,
        );
        let acts = match decided {
            Ok(acts) => acts,
            Err(refusal) => {
                let words = refusal.words(&|root| text(&shelf.place(root, &skill)));
                return Ok(Err(ApiError::new(StatusCode::CONFLICT, words)));
            }
        };
        let undo = match shelf.apply(&skill, &acts) {
            Ok(undo) => undo,
            Err(error) => {
                let why = error.to_string();
                return Ok(Err(ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, why)));
            }
        };
        let told = rules::changed(&item.unwrap_or_default(), enabled);
        Ok::<_, Malformed>(Ok((told, undo, skill)))
    })
    .await?;
    let (told, undo, skill) = worked.map_err(|Malformed| unavailable())??;
    let after = rules::chosen_after(&before, &skill, enabled);
    if let Err(error) = keep_chosen(app, &after).await {
        // The list could not be kept: the links go back to how they were.
        let _ = blocking(move || undo.run()).await;
        return Err(error);
    }
    Ok(told)
}

/// At the start of a Host that keeps the machine's tools: the chosen list is taken the first
/// time (the other Host's, else the release's defaults), the core skills a newer release
/// brought are chosen, and every chosen skill is linked to this release where nothing or
/// another release's link is. A Host that does not keep the tools does nothing here.
pub async fn at_start(app: &App) {
    let Some(shelf) = Shelf::of(&app.config) else {
        return;
    };
    let before_file = app.config.skills_chosen_before.clone();
    let kept = chosen(app);
    let was = kept.clone();
    let worked = tokio::task::spawn_blocking(move || {
        let items = shelf.items()?;
        let first = match kept {
            Some(kept) => kept,
            None => {
                let other = before_file
                    .and_then(|file| std::fs::read(file).ok())
                    .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
                rules::first_chosen(other.as_ref(), &items)?
            }
        };
        let chosen = rules::enrolled(&first, &items)?;
        let roots = shelf.written_roots();
        let mut failed = Vec::new();
        for skill in &chosen {
            let in_release = rules::entry(&items, skill)?.is_some() && shelf.has_source(skill);
            let acts: Vec<(Root, Act)> = roots
                .iter()
                .map(|root| (*root, rules::at_start(shelf.link(*root, skill), in_release)))
                .collect();
            if let Err(error) = shelf.apply(skill, &acts) {
                failed.push(format!("{skill}: {error}"));
            }
        }
        Ok::<_, Malformed>((chosen, failed))
    })
    .await;
    match worked {
        Ok(Ok((chosen, failed))) => {
            for why in failed {
                eprintln!("clarp-host: a skill could not be linked: {why}");
            }
            if was.as_ref() == Some(&chosen) {
                return;
            }
            if let Err(error) = keep_chosen(app, &chosen).await {
                eprintln!(
                    "clarp-host: the chosen skills were not kept: {}",
                    error.message()
                );
            }
        }
        Ok(Err(Malformed)) => eprintln!("clarp-host: {}", rules::UNAVAILABLE),
        Err(error) => eprintln!("clarp-host: the skills were not linked: {error}"),
    }
}

/// Whether this Host keeps the machine's tools' skills: what lists `managed_skills`.
pub fn keeps(config: &Config) -> bool {
    config.accounts == Accounts::Select && config.skills.is_some()
}

pub fn routes() -> Router<App> {
    Router::new().route("/managed-skills", get(list).post(change))
}
