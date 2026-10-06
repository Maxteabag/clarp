//! Agent portraits as drawn: round, at exactly the device pixels they
//! cover (the size setting × the interface and monitor scale), with soft
//! edges made here, because the software renderer neither smooths a
//! scaled image nor clips round. Each size is made once from the Host's
//! portrait on the decoder thread and cached beside it on disk, so a usual
//! launch decodes small files only. The rings round avatars (an initial's
//! outline, a status) are soft masks made the same way, coloured by the
//! theme in Slint.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use clarp_core::media::{device_side, sized_portrait_path};
use clarp_engine::Change;
use slint::ComponentHandle;

use crate::{App, AppWindow, AvatarArt};

/// The avatar sizes, in logical pixels: an explorer row, a compact row,
/// the profile header and an overview card.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sizes {
    pub row: f32,
    pub compact: f32,
    pub card: f32,
}

/// The size setting's choices (`appearance/avatarSize`), smallest first,
/// with their labels and sizes; medium is the default.
pub const CHOICES: &[(&str, &str, Sizes)] = &[
    ("small", "Small", Sizes { row: 36.0, compact: 28.0, card: 34.0 }),
    ("medium", "Medium", Sizes { row: 44.0, compact: 32.0, card: 40.0 }),
    ("large", "Large", Sizes { row: 52.0, compact: 38.0, card: 48.0 }),
];
const DEFAULT: usize = 1;

fn choice(app: &App) -> usize {
    let settings = app.engine.borrow();
    let id = settings.settings().get("appearance/avatarSize").and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default();
    CHOICES.iter().position(|(c, _, _)| *c == id).unwrap_or(DEFAULT)
}

pub fn sizes(app: &App) -> Sizes {
    CHOICES[choice(app)].2
}

/// The setting's label, for its Settings row ("Medium · 44 px").
pub fn label(app: &App) -> String {
    let (_, label, sizes) = CHOICES[choice(app)];
    format!("{label} · {} px", sizes.row)
}

pub fn current(app: &App) -> &'static str {
    CHOICES[choice(app)].0
}

/// Chooses a size by id (Ctrl+K), or the next (`delta` 1) or previous one.
pub fn set(app: &App, window: &AppWindow, id: Option<&str>, delta: i32) {
    let index = match id {
        Some(id) => match CHOICES.iter().position(|(c, _, _)| *c == id) {
            Some(index) => index,
            None => {
                eprintln!("clarp-slint: no avatar size {id}");
                return;
            }
        },
        None => (choice(app) as i32 + delta).rem_euclid(CHOICES.len() as i32) as usize,
    };
    app.engine.borrow_mut().settings_mut().set("appearance/avatarSize", CHOICES[index].0);
    apply(app, window);
    refresh_for_portraits("avatar size changed".into());
}

/// Shows the chosen sizes and makes the rings (at startup and on a change).
pub fn apply(app: &App, window: &AppWindow) {
    let sizes = sizes(app);
    let art = window.global::<AvatarArt>();
    art.set_row(sizes.row);
    art.set_compact(sizes.compact);
    art.set_card(sizes.card);
    art.on_ring(|size, stroke, _| ring(size, stroke));
}

/// The interface or the monitor scale changed, or the explorer turned
/// compact: rings and portraits are made again for the new device size.
pub fn remake() {
    let Some(window) = crate::window() else { return };
    let art = window.global::<AvatarArt>();
    art.set_revision(art.get_revision() + 1);
    // After the scale change is handled, not inside it.
    slint::Timer::single_shot(std::time::Duration::ZERO, || refresh_for_portraits("avatar device size changed".into()));
}

fn scale() -> f32 {
    crate::window().map_or(1.0, |w| w.window().scale_factor())
}

#[derive(Default)]
struct State {
    /// Round portraits by their sized file's path.
    images: HashMap<String, slint::Image>,
    /// The latest one made for each portrait, by its sources: shown
    /// (scaled) while the new size is being made, not the initial.
    latest: HashMap<String, slint::Image>,
    /// Portraits the decoder has and has not delivered yet.
    decoding: HashSet<String>,
    /// Of those, the ones a list asked for: their arrival refreshes it.
    wanted: HashSet<String>,
    /// The paced refresh for arrived portraits is running (`portraits_due`).
    due: bool,
    /// Soft rings by (device side, stroke in hundredths of a pixel).
    rings: HashMap<(u32, u32), slint::Image>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

/// What the decoder makes: `sources`' round portrait at `side` px,
/// cached at `path`.
struct Job {
    path: PathBuf,
    sources: Vec<PathBuf>,
    side: u32,
}

impl Job {
    fn key(&self) -> String {
        self.path.to_string_lossy().into_owned()
    }

    fn of(sources: &[String], logical: f32) -> Option<Self> {
        let sources: Vec<PathBuf> = sources.iter().map(|url| crate::profile_view::file_path(url)).collect::<Option<_>>()?;
        let side = device_side(logical, scale());
        let refs: Vec<&std::path::Path> = sources.iter().map(PathBuf::as_path).collect();
        Some(Job { path: sized_portrait_path(&refs, side), sources, side })
    }
}

/// The agent's portrait `logical` px wide (fetched on first use). Made on
/// the decoder, so a hundred of them never hold the UI thread: empty (the
/// initial shows) until it is there; its arrival refreshes the lists.
pub fn portrait(app: &App, session: &str, logical: f32) -> slint::Image {
    let Some(url) = app.engine.borrow_mut().avatar_source(session) else { return slint::Image::default() };
    sized(&[url], logical)
}

/// A pair room's portrait: both agents' in one circle.
pub fn pair_portrait(app: &App, left: &str, right: &str, logical: f32) -> slint::Image {
    let urls: Vec<String> = [left, right].iter().filter_map(|s| app.engine.borrow_mut().avatar_source(s)).collect();
    if urls.len() != 2 {
        return slint::Image::default();
    }
    sized(&urls, logical)
}

fn sized(urls: &[String], logical: f32) -> slint::Image {
    let Some(job) = Job::of(urls, logical) else {
        eprintln!("clarp-slint: a portrait is not a local file: {urls:?}");
        return slint::Image::default();
    };
    let (key, sources) = (job.key(), urls.join("\n"));
    let found = STATE.with(|s| {
        let mut state = s.borrow_mut();
        if let Some(image) = state.images.get(&key) {
            let image = image.clone();
            state.latest.insert(sources.clone(), image.clone());
            return Ok(image);
        }
        state.wanted.insert(key.clone());
        Err(state.latest.get(&sources).cloned().unwrap_or_default())
    });
    found.unwrap_or_else(|meanwhile| {
        decode_later(vec![job]);
        meanwhile
    })
}

/// Whether a portraits-only change rebuilds the lists now: only in the
/// paced refresh for arrived portraits. Otherwise the new ones are made
/// first, or the paced refresh is asked for when they already are.
pub fn portraits_due(app: &App, compact: bool) -> bool {
    if STATE.with(|s| std::mem::take(&mut s.borrow_mut().due)) {
        return true;
    }
    let sizes = sizes(app);
    let logical = if compact { sizes.compact } else { sizes.row };
    let sessions: Vec<String> = app.engine.borrow().roster().agents().iter().map(|a| a.session.clone()).collect();
    let jobs: Vec<Job> =
        sessions.iter().filter_map(|s| app.engine.borrow_mut().avatar_source(s)).filter_map(|url| Job::of(&[url], logical)).collect();
    let missing: Vec<Job> = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let missing: Vec<Job> = jobs.into_iter().filter(|j| !state.images.contains_key(&j.key())).collect();
        state.wanted.extend(missing.iter().map(Job::key));
        missing
    });
    if missing.is_empty() {
        refresh_for_portraits("new portraits already made".into());
    }
    decode_later(missing);
    false
}

/// Decodes the portraits made at the last launch (newest first) while the
/// Host is asked for the roster, so a usual launch lists every agent with
/// its portrait.
pub fn prewarm_portraits() {
    let Some(folder) = clarp_core::media::cache_dir().map(|c| c.join("portraits").join("sources")) else { return };
    let Ok(entries) = std::fs::read_dir(&folder) else { return };
    let mut files: Vec<(std::time::SystemTime, PathBuf)> = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| clarp_core::media::is_sized_portrait(p))
        .filter_map(|p| Some((p.metadata().ok()?.modified().ok()?, p)))
        .collect();
    files.sort_by(|a, b| b.0.cmp(&a.0));
    // Enough for a large roster at a size or two.
    let jobs = files.into_iter().take(400).map(|(_, path)| Job { path, sources: Vec::new(), side: 0 }).collect();
    decode_later(jobs);
}

/// Each job's key, its portrait, and whether it could have been made (a
/// prewarmed file that cannot be read is made again when asked for).
type Made = Vec<(String, Option<slint::SharedPixelBuffer<slint::Rgba8Pixel>>, bool)>;

/// Hands `jobs` to the decoder thread, each once.
fn decode_later(jobs: Vec<Job>) {
    use std::sync::{Mutex, OnceLock, mpsc};
    static DECODER: OnceLock<Mutex<mpsc::Sender<Job>>> = OnceLock::new();
    let jobs: Vec<Job> = STATE.with(|s| {
        let mut state = s.borrow_mut();
        jobs.into_iter().filter(|j| !state.images.contains_key(&j.key()) && state.decoding.insert(j.key())).collect()
    });
    if jobs.is_empty() {
        return;
    }
    let decoder = DECODER.get_or_init(|| {
        let (sender, receiver) = mpsc::channel::<Job>();
        let spawned = std::thread::Builder::new().name("portraits".into()).spawn(move || {
            while let Ok(first) = receiver.recv() {
                // What queued meanwhile goes in the same batch: one list
                // refresh for many portraits.
                let started = std::time::Instant::now();
                let batch: Made = std::iter::once(first).chain(receiver.try_iter()).map(|job| (job.key(), make(&job), !job.sources.is_empty())).collect();
                let took = started.elapsed();
                if let Err(error) = slint::invoke_from_event_loop(move || made(batch, took)) {
                    eprintln!("clarp-slint: dropped decoded portraits: {error}");
                }
            }
        });
        if let Err(error) = spawned {
            eprintln!("clarp-slint: no portrait decoder: {error}");
        }
        Mutex::new(sender)
    });
    match decoder.lock() {
        Ok(sender) => {
            for job in jobs {
                if sender.send(job).is_err() {
                    eprintln!("clarp-slint: the portrait decoder stopped");
                    break;
                }
            }
        }
        Err(error) => eprintln!("clarp-slint: the portrait decoder is poisoned: {error}"),
    }
}

/// On the decoder: the cached round portrait, else one made from its
/// sources and cached.
fn make(job: &Job) -> Option<slint::SharedPixelBuffer<slint::Rgba8Pixel>> {
    let pixels = |image: image::RgbaImage| slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(image.as_raw(), image.width(), image.height());
    if job.path.exists() {
        match image::open(&job.path) {
            Ok(image) => return Some(pixels(image.to_rgba8())),
            // Made again below, when its sources are known.
            Err(error) => eprintln!("clarp-slint: cannot load {}: {error}", job.path.display()),
        }
    }
    if job.sources.is_empty() {
        return None;
    }
    let mut sources = Vec::new();
    for path in &job.sources {
        match image::open(path) {
            Ok(image) => sources.push(image.to_rgba8()),
            Err(error) => {
                eprintln!("clarp-slint: cannot load the portrait {}: {error}", path.display());
                return None;
            }
        }
    }
    let portrait = match sources.as_slice() {
        [one] => clarp_core::media::round_portrait(one, job.side),
        [left, right] => clarp_core::media::pair_portrait(left, right, job.side),
        _ => return None,
    };
    match clarp_core::media::png(&portrait).map(|png| std::fs::write(&job.path, png)) {
        Some(Ok(())) => {}
        Some(Err(error)) => eprintln!("clarp-slint: cannot cache {}: {error}", job.path.display()),
        None => eprintln!("clarp-slint: cannot encode {}", job.path.display()),
    }
    Some(pixels(portrait))
}

/// On the UI thread: the decoder's batch joins the cache, and lists that
/// show one of them refresh once.
fn made(batch: Made, took: std::time::Duration) {
    let count = batch.len();
    let wanted = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let mut wanted = false;
        for (key, pixels, settled) in batch {
            state.decoding.remove(&key);
            wanted |= state.wanted.remove(&key);
            // One that could not be made stays empty (the initial shows).
            if pixels.is_some() || settled {
                state.images.insert(key, pixels.map(slint::Image::from_rgba8).unwrap_or_default());
            }
        }
        wanted
    });
    if wanted {
        refresh_for_portraits(format!("{count} portraits decoded in {:.1} ms", crate::perf::ms(took)));
    }
}

/// Refreshes the lists for arrived portraits, at most every 100 ms: at a
/// first launch they arrive a few at a time for a while.
fn refresh_for_portraits(why: String) {
    use std::time::{Duration, Instant};
    const EVERY: Duration = Duration::from_millis(100);
    thread_local! {
        /// The last refresh, and whether one is already scheduled.
        static PACE: std::cell::Cell<(Option<Instant>, bool)> = const { std::cell::Cell::new((None, false)) };
    }
    let (last, scheduled) = PACE.get();
    if scheduled {
        return;
    }
    let refresh = move || {
        PACE.set((Some(Instant::now()), false));
        if let Some(app) = crate::app() {
            let started = Instant::now();
            STATE.with(|s| s.borrow_mut().due = true);
            app.refresh(&[Change::Avatars]);
            STATE.with(|s| s.borrow_mut().due = false);
            crate::perf::woke(started, why);
        }
    };
    match last.map_or(Duration::ZERO, |at| EVERY.saturating_sub(at.elapsed())) {
        wait if wait.is_zero() => refresh(),
        wait => {
            PACE.set((last, true));
            slint::Timer::single_shot(wait, refresh);
        }
    }
}

/// A soft white ring `stroke` wide inside a `size` square (logical px),
/// made at the device size it is drawn at.
fn ring(size: f32, stroke: f32) -> slint::Image {
    let scale = scale();
    let side = device_side(size, scale);
    let stroke = (stroke * scale).max(1.0);
    let key = (side, (stroke * 100.0).round() as u32);
    STATE.with(|s| {
        s.borrow_mut()
            .rings
            .entry(key)
            .or_insert_with(|| {
                let mask = clarp_core::media::ring_mask(side, stroke);
                slint::Image::from_rgba8(slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(mask.as_raw(), side, side))
            })
            .clone()
    })
}
