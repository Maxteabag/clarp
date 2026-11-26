//! The skills this Host keeps installed for its agents' tools (`GET` and `POST
//! /managed-skills`), and the links it puts in place beside its start.
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
    /// The releases the links of Clarp's lead into, as the last reconciliation or change left
    /// them (issue 935): install-host keeps every one of them. A Host from before writes none.
    linked_releases: Vec<String>,
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
        self.keep_marked(path, &mut Steps::none())
    }

    /// The same, its write, flush and rename each marked.
    fn keep_marked(&self, path: &Path, steps: &mut Steps) -> io::Result<()> {
        let beside = path.with_file_name(format!(".install.json.{}.next", crate::ids::hex(8)));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&beside)?;
        let write = (|| {
            serde_json::to_writer(&mut file, self)?;
            file.write_all(b"\n")?;
            steps.mark("write");
            file.sync_all()?;
            steps.mark("sync");
            std::fs::rename(&beside, path)?;
            steps.mark("rename");
            Ok(())
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
    status(&app)
        .await
        .map(|rows| Json(json!({ "skills": rows })))
}

pub(crate) async fn status(app: &App) -> Result<Vec<Value>, ApiError> {
    let Some(shelf) = Shelf::of(&app.config) else {
        return Ok(Vec::new());
    };
    let _one = app.skill_links.lock().await;
    let path = app.config.data_dir.join("install.json");
    let seed = app.config.skills_chosen_before.clone();
    blocking(move || {
        let state = InstallState::load(&path, seed.as_deref()).map_err(disk_error)?;
        shelf
            .status(&state.skills_off)
            .map_err(|Malformed| unavailable())
    })
    .await?
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
        state.linked_releases = shelf.linked_releases(&shelf.written_roots());
        if let Err(error) = state.keep(&path) {
            undo.run();
            return Err(disk_error(error));
        }
        Ok(rules::changed(item.unwrap_or(&Value::Null), enabled))
    })
    .await?
}

/// Reconciles the shipped skill tree at every start of the Host that keeps the tools (issue
/// 935), beside the start rather than before it is done. The lock over the tools' links is taken
/// here, before the start is done, so every `GET` and `POST /managed-skills` (held at the door
/// until then) comes after the reconciliation and none runs beside it: one writer at a time.
/// Until the links have moved, a turn reads the release before's skills, which install-host
/// keeps while a link leads into it (`linked_releases`). A reconciliation that could not put
/// every link in place is tried again, the lock let go in between, until it has.
pub async fn at_start(app: &App) {
    let mut steps = Steps::from(std::time::Instant::now());
    let Some(shelf) = Shelf::of(&app.config) else {
        return;
    };
    steps.mark("shelf");
    let held = app.skill_links.clone().lock_owned().await;
    steps.mark("lock");
    let app = app.clone();
    tokio::spawn(keep_reconciling(
        app,
        std::sync::Arc::new(shelf),
        held,
        steps,
    ));
}

/// How long each step of one reconciliation took, by the monotonic clock, for its journal line
/// (issue 935): where a slow one spent its time, on an ordinary start.
struct Steps {
    began: Option<std::time::Instant>,
    last: std::time::Instant,
    said: Vec<(&'static str, std::time::Duration)>,
}

impl Steps {
    fn from(began: std::time::Instant) -> Self {
        Self {
            began: Some(began),
            last: began,
            said: Vec::new(),
        }
    }

    /// Marks that are kept nowhere.
    fn none() -> Self {
        Self {
            began: None,
            last: std::time::Instant::now(),
            said: Vec::new(),
        }
    }

    fn mark(&mut self, step: &'static str) {
        if self.began.is_none() {
            return;
        }
        let now = std::time::Instant::now();
        self.said.push((step, now - self.last));
        self.last = now;
    }

    /// The whole, from the first mark's beginning, in ms.
    fn ms(&self) -> u128 {
        self.began
            .map_or(0, |began| (self.last - began).as_millis())
    }

    /// `shelf=0.1 lock=0.0 ...`: each step in ms.
    fn words(&self) -> String {
        let said: Vec<String> = self
            .said
            .iter()
            .map(|(step, took)| format!("{step}={:.1}", took.as_secs_f64() * 1000.0))
            .collect();
        said.join(" ")
    }
}

/// Why a reconciliation was not done.
enum NotReconciled {
    /// The release's manifest cannot be read: no try again changes that.
    Manifest,
    Disk(io::Error),
}

/// What one reconciliation did.
struct Reconciled {
    linked: usize,
    unlinked: usize,
    /// Each skill whose links could not be put right, and why.
    failed: Vec<String>,
    releases: usize,
}

async fn keep_reconciling(
    app: App,
    shelf: std::sync::Arc<Shelf>,
    mut held: tokio::sync::OwnedMutexGuard<()>,
    mut steps: Steps,
) {
    let mut again = std::time::Duration::from_secs(1);
    let mut first = true;
    loop {
        let path = app.config.data_dir.join("install.json");
        let seed = app.config.skills_chosen_before.clone();
        let work = shelf.clone();
        let (done, mut steps_done) = tokio::task::spawn_blocking(move || {
            steps.mark("enter");
            let done = work.reconcile(&path, seed.as_deref(), &mut steps);
            (done, steps)
        })
        .await
        .unwrap_or_else(|panic| {
            let why = io::Error::other(panic.to_string());
            (Err(NotReconciled::Disk(why)), Steps::none())
        });
        drop(held);
        steps_done.mark("back");
        let (ms, marks) = (steps_done.ms(), steps_done.words());
        match done {
            Ok(done) if done.failed.is_empty() => {
                eprintln!(
                    "clarp-host: skills reconciled in {ms} ms: {} links put in place, {} taken \
                     away; the links lead into {} releases; steps ms: {marks}",
                    done.linked, done.unlinked, done.releases
                );
                return;
            }
            Ok(done) => {
                // Why each could not be is said at the first try; a try again says how many.
                if first {
                    for why in &done.failed {
                        eprintln!("clarp-host: a skill could not be linked: {why}");
                    }
                }
                eprintln!(
                    "clarp-host: the skills were reconciled in part in {ms} ms: {} could not be \
                     put right, tried again in {} s; steps ms: {marks}",
                    done.failed.len(),
                    again.as_secs()
                );
            }
            Err(NotReconciled::Manifest) => {
                eprintln!(
                    "clarp-host: the skills were not reconciled: {}",
                    rules::UNAVAILABLE
                );
                return;
            }
            Err(NotReconciled::Disk(error)) => eprintln!(
                "clarp-host: the skills were not reconciled: {error}; tried again in {} s; steps \
                 ms: {marks}",
                again.as_secs()
            ),
        }
        first = false;
        tokio::time::sleep(again).await;
        again = (again * 2).min(std::time::Duration::from_secs(300));
        steps = Steps::from(std::time::Instant::now());
        held = app.skill_links.clone().lock_owned().await;
        steps.mark("lock");
    }
}

impl Shelf {
    /// One reconciliation of the tools' links with the release's on-list, under the lock.
    fn reconcile(
        &self,
        path: &Path,
        seed: Option<&Path>,
        steps: &mut Steps,
    ) -> Result<Reconciled, NotReconciled> {
        // The tests hold the reconciliation while the file named exists (issue 935).
        #[cfg(debug_assertions)]
        if let Some(hold) = std::env::var_os("CLARP_TEST_SKILLS_HELD_WHILE") {
            while Path::new(&hold).exists() {
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
            steps.mark("held");
        }
        let items = self.items().map_err(|Malformed| NotReconciled::Manifest)?;
        steps.mark("manifest");
        let mut state = InstallState::load(path, seed).map_err(NotReconciled::Disk)?;
        state.skills = rules::enrolled(&state.skills_off, &items)
            .map_err(|Malformed| NotReconciled::Manifest)?;
        steps.mark("choices");
        let roots = self.written_roots();

        // Include links left by old releases even when they are absent from the previous
        // on-list. Link ownership is checked before any of them can be removed.
        let mut candidates: BTreeSet<String> = state.skills.iter().cloned().collect();
        for root in &roots {
            candidates.extend(self.links_in(*root));
        }
        steps.mark("roots");
        let mut done = Reconciled {
            linked: 0,
            unlinked: 0,
            failed: Vec::new(),
            releases: 0,
        };
        for skill in candidates {
            let enabled = state.skills.contains(&skill) && self.has_source(&skill);
            let acts: Vec<(Root, Act)> = roots
                .iter()
                .map(|root| (*root, rules::at_start(self.link(*root, &skill), enabled)))
                .collect();
            match self.apply(&skill, &acts) {
                Ok(_) => {
                    for (_, act) in &acts {
                        match act {
                            Act::Link => done.linked += 1,
                            Act::Unlink => done.unlinked += 1,
                            Act::Leave => {}
                        }
                    }
                }
                Err(error) => done.failed.push(format!("{skill}: {error}")),
            }
        }
        steps.mark("links");
        state.linked_releases = self.linked_releases(&roots);
        done.releases = state.linked_releases.len();
        steps.mark("scan");
        state
            .keep_marked(path, steps)
            .map_err(NotReconciled::Disk)?;
        Ok(done)
    }

    /// The names of the links in a tool's folder.
    fn links_in(&self, root: Root) -> Vec<String> {
        let Ok(entries) = std::fs::read_dir(self.folder(root)) else {
            return Vec::new();
        };
        entries
            .flatten()
            .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_symlink()))
            .filter_map(|entry| entry.file_name().into_string().ok())
            .collect()
    }

    /// The releases of Clarp the links in the tools' folders lead into now, resolved: this
    /// release's and any other a link of Clarp's still reads its skill from. install-host keeps
    /// each of them, so that no link of Clarp's leads to a release it has taken away.
    fn linked_releases(&self, roots: &[Root]) -> Vec<String> {
        let mut releases = BTreeSet::new();
        for root in roots {
            for skill in self.links_in(*root) {
                if matches!(self.link(*root, &skill), Link::Healthy | Link::Outdated) {
                    let resolved = resolve(&self.place(*root, &skill));
                    if let Some(release) = resolved.parent().and_then(Path::parent) {
                        releases.insert(text(release));
                    }
                }
            }
        }
        releases.into_iter().collect()
    }
}

/// Whether this Host keeps the machine's tools' skills: what lists `managed_skills`.
pub fn keeps(config: &Config) -> bool {
    config.accounts == Accounts::Select && config.skills.is_some()
}

pub fn routes() -> Router<App> {
    Router::new().route("/managed-skills", get(list).post(change))
}
