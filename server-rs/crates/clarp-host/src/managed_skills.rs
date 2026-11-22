//! The skills this Host keeps installed for its agents' tools (`GET` and `POST
//! /managed-skills`), and the links it puts in place at its start.
//!
//! The rules are the kernel's (`clarp_kernel::managed_skills`, held to the Python Host); this
//! module finds what is on the disk, asks the kernel, and does what it says. One Host keeps a
//! machine's tools (the lead's ruling of 20 Nov 2026): this one only where it was started with
//! `--accounts select` and a tree of skills (`--skills`). Anywhere else it reads no tool's
//! folder, answers the list empty, refuses every change in one sentence and writes nothing,
//! at a request or at its start.

use std::collections::BTreeSet;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
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
use serde::{Deserialize, Serialize};
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
        rules::manifest_noting(bytes.as_deref(), |index, problem| {
            eprintln!("clarp-host: managedSkillsManifestBad item={index} {problem}");
        })
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
    fn status(&self, off: &[String]) -> Result<Vec<Value>, Malformed> {
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
                enabled: rules::enabled(&item, &skill, off),
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

/// This Host's install record. The previous Host's file is a read-only seed.
#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
struct InstallState {
    skills: Vec<String>,
    skills_off: Vec<String>,
}

impl InstallState {
    fn load(path: &Path, seed: Option<&Path>) -> io::Result<Self> {
        match std::fs::read(path) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let other = seed
                    .and_then(|file| std::fs::read(file).ok())
                    .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
                Ok(Self {
                    skills_off: rules::first_off(other.as_ref()),
                    ..Self::default()
                })
            }
            Err(error) => Err(error),
        }
    }

    fn keep(&self, path: &Path) -> io::Result<()> {
        let beside = path.with_file_name(format!(".install.json.{}.next", crate::ids::hex(8)));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&beside)?;
        let write = (|| {
            serde_json::to_writer(&mut file, self)?;
            file.write_all(b"\n")?;
            file.sync_all()?;
            std::fs::rename(&beside, path)
        })();
        if write.is_err() {
            let _ = std::fs::remove_file(beside);
        }
        write
    }
}

fn disk_error(error: io::Error) -> ApiError {
    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
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
    let _one = app.skill_links.lock().await;
    let path = app.config.data_dir.join("install.json");
    let seed = app.config.skills_chosen_before.clone();
    let rows = blocking(move || {
        let state = InstallState::load(&path, seed.as_deref()).map_err(disk_error)?;
        shelf
            .status(&state.skills_off)
            .map_err(|Malformed| unavailable())
    })
    .await??;
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
    let _one = app.skill_links.lock().await;
    let path = app.config.data_dir.join("install.json");
    let seed = app.config.skills_chosen_before.clone();
    blocking(move || {
        let items = shelf.items().map_err(|Malformed| unavailable())?;
        let item = rules::entry(&items, &skill).map_err(|Malformed| unavailable())?;
        let mut state = InstallState::load(&path, seed.as_deref()).map_err(disk_error)?;
        let roots: Vec<(Root, Link)> = shelf
            .written_roots()
            .into_iter()
            .map(|root| (root, shelf.link(root, &skill)))
            .collect();
        let acts = rules::change(item, &skill, enabled, shelf.has_source(&skill), &roots).map_err(
            |refusal| {
                let words = refusal.words(&|root| text(&shelf.place(root, &skill)));
                ApiError::new(StatusCode::CONFLICT, words)
            },
        )?;
        state.skills_off = rules::chosen_after(&state.skills_off, &skill, !enabled);
        state.skills =
            rules::enrolled(&state.skills_off, &items).map_err(|Malformed| unavailable())?;
        let undo = shelf.apply(&skill, &acts).map_err(disk_error)?;
        if let Err(error) = state.keep(&path) {
            undo.run();
            return Err(disk_error(error));
        }
        Ok(rules::changed(item.unwrap_or(&Value::Null), enabled))
    })
    .await?
}

/// Reconcile the shipped skill tree at every start of the Host that keeps the tools.
pub async fn at_start(app: &App) {
    let Some(shelf) = Shelf::of(&app.config) else {
        return;
    };
    let _one = app.skill_links.lock().await;
    let path = app.config.data_dir.join("install.json");
    let seed = app.config.skills_chosen_before.clone();
    let worked = blocking(move || {
        let items = shelf.items().map_err(|Malformed| unavailable())?;
        let mut state = InstallState::load(&path, seed.as_deref()).map_err(disk_error)?;
        state.skills =
            rules::enrolled(&state.skills_off, &items).map_err(|Malformed| unavailable())?;
        let roots = shelf.written_roots();

        // Include links left by old releases even when they are absent from the previous
        // on-list. Link ownership is checked before any of them can be removed.
        let mut candidates: BTreeSet<String> = state.skills.iter().cloned().collect();
        for root in &roots {
            if let Ok(entries) = std::fs::read_dir(shelf.folder(*root)) {
                candidates.extend(entries.flatten().filter_map(|entry| {
                    if entry.file_type().is_ok_and(|kind| kind.is_symlink()) {
                        entry.file_name().into_string().ok()
                    } else {
                        None
                    }
                }));
            }
        }
        for skill in candidates {
            let enabled = state.skills.contains(&skill) && shelf.has_source(&skill);
            let acts: Vec<(Root, Act)> = roots
                .iter()
                .map(|root| (*root, rules::at_start(shelf.link(*root, &skill), enabled)))
                .collect();
            if let Err(error) = shelf.apply(&skill, &acts) {
                eprintln!("clarp-host: a skill could not be linked: {skill}: {error}");
            }
        }
        state.keep(&path).map_err(disk_error)
    })
    .await;
    if let Err(error) = worked.and_then(|result| result) {
        eprintln!(
            "clarp-host: the skills were not reconciled: {}",
            error.message()
        );
    }
}

/// Whether this Host keeps the machine's tools' skills: what lists `managed_skills`.
pub fn keeps(config: &Config) -> bool {
    config.accounts == Accounts::Select && config.skills.is_some()
}

pub fn routes() -> Router<App> {
    Router::new().route("/managed-skills", get(list).post(change))
}
