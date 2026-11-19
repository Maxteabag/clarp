//! The Host's own engine that listens: a worker process that keeps a Whisper model loaded and
//! hears one clip at a time.
//!
//! The model is hundreds of megabytes and takes seconds to load, so the worker is started
//! once and kept. It is a process of its own because hearing a clip is minutes of processor
//! time for a long one, and because what runs the model is not this Host's code: a worker
//! that falls over takes nothing with it, and the next clip starts another.
//!
//! The Host and the worker write each other lines of JSON, the Python Host's own protocol
//! (`server/lib/stt_worker.py`), so that either Host's worker serves:
//!
//! ```text
//! -> {"op":"load","model_source":...,"compute_type":...,"cpu_threads":N,"language":...}
//! <- {"event":"ready"} | {"event":"load_error","error":"..."}
//! -> {"op":"transcribe","id":"...","path":"/.../clip.m4a","prompt":"...","beam_size":1}
//! <- {"id":"...","segments":[{"text":"...","no_speech_prob":0.1}]} | {"id":"...","error":"..."}
//! ```
//!
//! This Host's worker also takes a `language` with a clip, for an agent that is spoken to in
//! another language than the Host's own.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::listen::{Asked, Clip, Deaf, Heard, Listener, Segment, suffix};

/// How a worker is started and what it loads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Worker {
    /// The program that runs the worker, and what it is given: an interpreter and the
    /// worker's script, say.
    pub program: PathBuf,
    pub args: Vec<String>,
    pub env: Vec<(String, String)>,
    /// The model: a name the worker's library knows (`small.en`), or a directory that
    /// holds one.
    pub model: String,
    /// How the model's numbers are kept in memory (`int8`).
    pub compute_type: String,
    /// How many threads the model may use. 0: the library's own choice.
    pub cpu_threads: u32,
    /// The language the model is loaded for. `None`: it finds the language itself.
    pub language: Option<String>,
    /// How wide the search for the words is. 1 is the fastest.
    pub beam_size: u32,
    /// Where a clip is written for the worker to read. What is written there is removed
    /// when the clip was heard.
    pub scratch: PathBuf,
    /// How long a model may take to load.
    pub load_within: Duration,
    /// How long one clip may take to hear.
    pub hear_within: Duration,
}

/// The threads a model is given on a machine with this many cores: half of them, four at
/// most, one at least. The Python Host's rule (`inference_cpu_threads`): a model left to
/// take every core starves the server that answers the apps beside it.
#[must_use]
pub fn threads_for(cores: usize) -> u32 {
    u32::try_from((cores / 2).clamp(1, 4)).unwrap_or(1)
}

impl Worker {
    pub fn new(program: impl Into<PathBuf>, model: impl Into<String>) -> Self {
        Self {
            program: program.into(),
            args: Vec::new(),
            env: Vec::new(),
            model: model.into(),
            compute_type: "int8".to_string(),
            cpu_threads: 0,
            language: None,
            beam_size: 1,
            scratch: std::env::temp_dir(),
            load_within: Duration::from_secs(600),
            hear_within: Duration::from_secs(900),
        }
    }
}

/// How long a worker that could not be started is left alone before it is tried again.
const TRIED_AGAIN_AFTER: Duration = Duration::from_secs(30);

/// What is said when the worker is gone in the middle of something.
const ENDED: &str = "the transcription worker ended";
/// What is said when the worker took longer than a clip may take.
const SILENT: &str = "the transcription worker did not answer in time";

/// A worker that runs.
struct Running {
    child: Child,
    stdin: ChildStdin,
    /// What the worker writes, a line at a time. It ends when the worker does.
    lines: Receiver<String>,
}

impl Running {
    fn say(&mut self, line: &Value) -> std::io::Result<()> {
        writeln!(self.stdin, "{line}")?;
        self.stdin.flush()
    }
    fn reply(&mut self, id: &str, within: Duration) -> Result<Value, Unheard> {
        let until = Instant::now() + within;
        loop {
            let left = until.saturating_duration_since(Instant::now());
            let line = match self.lines.recv_timeout(left) {
                Ok(line) => line,
                Err(RecvTimeoutError::Timeout) => return Err(Unheard::Gone(SILENT)),
                Err(RecvTimeoutError::Disconnected) => return Err(Unheard::Gone(ENDED)),
            };
            let Ok(said) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if said["id"].as_str() != Some(id) {
                continue; // the answer to a clip nobody waits for any more
            }
            if let Some(why) = said["error"].as_str() {
                return Err(Unheard::Said(why.to_string()));
            }
            return Ok(said);
        }
    }
}

/// A worker nobody holds any more is ended: none is left behind, whoever let go of it.
impl Drop for Running {
    fn drop(&mut self) {
        // Asked nicely first: it may be in the middle of nothing, and ends at once.
        let _ = writeln!(self.stdin, "{}", json!({ "op": "quit" }));
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Why a worker gave no pieces for a clip.
enum Unheard {
    /// It said why, and is as good as before.
    Said(String),
    /// It is gone or stuck, and is not kept.
    Gone(&'static str),
}

enum State {
    /// Not started, or ended: it is started when it is next needed.
    Down,
    /// Started, and loading its model.
    Loading,
    /// Loaded and free.
    Up(Running),
    /// Loaded, and hearing a clip.
    Hearing,
    /// It could not be started, or could not load its model. Said to whoever asks, and
    /// tried again after a while.
    Broken { why: String, at: Instant },
}

pub struct Whisper {
    worker: Worker,
    state: Arc<Mutex<State>>,
    clips: AtomicU64,
}

fn locked(state: &Mutex<State>) -> MutexGuard<'_, State> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Starts a worker and has it load its model.
fn load(worker: &Worker) -> std::io::Result<Running> {
    let mut command = clarp_child::command(&worker.program);
    command
        .args(&worker.args)
        .envs(worker.env.iter().map(|(name, value)| (name, value)))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = command.spawn().map_err(|error| {
        std::io::Error::other(format!(
            "the transcription worker could not be started ({}): {error}",
            worker.program.display()
        ))
    })?;
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(std::io::Error::other(
            "the transcription worker has no pipes",
        ));
    };
    let (sender, lines) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if sender.send(line).is_err() {
                break;
            }
        }
    });
    let mut running = Running {
        child,
        stdin,
        lines,
    };
    let asked = json!({
        "op": "load",
        "model_source": worker.model,
        "compute_type": worker.compute_type,
        "cpu_threads": worker.cpu_threads,
        "language": worker.language,
    });
    if running.say(&asked).is_err() {
        return Err(std::io::Error::other(format!(
            "{ENDED} before it loaded its model"
        )));
    }
    let until = Instant::now() + worker.load_within;
    loop {
        let left = until.saturating_duration_since(Instant::now());
        let line = match running.lines.recv_timeout(left) {
            Ok(line) => line,
            Err(RecvTimeoutError::Timeout) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "the transcription model took too long to load",
                ));
            }
            Err(RecvTimeoutError::Disconnected) => {
                return Err(std::io::Error::other(format!(
                    "{ENDED} while it loaded its model"
                )));
            }
        };
        let Ok(said) = serde_json::from_str::<Value>(&line) else {
            continue; // a library's own chatter
        };
        match said["event"].as_str() {
            Some("ready") => return Ok(running),
            Some("load_error") => {
                let why = said["error"].as_str().unwrap_or("unknown reason");
                // The first line says what failed; what follows is where in the worker.
                let first = why.lines().next().unwrap_or(why);
                return Err(std::io::Error::other(format!(
                    "the transcription model did not load: {first}"
                )));
            }
            _ => {}
        }
    }
}

impl Whisper {
    pub fn new(worker: Worker) -> Self {
        Self {
            worker,
            state: Arc::new(Mutex::new(State::Down)),
            clips: AtomicU64::new(0),
        }
    }

    /// Load an installed alternative on the calling thread. The caller serializes
    /// replacement so the child that used the old model has exited first.
    pub fn installed_variant(
        &self,
        id: &str,
        directory: &Path,
        language: Option<String>,
    ) -> Result<Self, Deaf> {
        let mut worker = self.worker.clone();
        if !directory.is_absolute() {
            return Err(Deaf::Failed(
                "an installed transcription model needs an absolute path".into(),
            ));
        }
        worker.model = directory
            .to_str()
            .ok_or_else(|| {
                Deaf::Failed("the installed transcription model path is not UTF-8".into())
            })?
            .to_owned();
        worker.language = language;
        worker.scratch = worker.scratch.join("variant");
        worker.env.push(("HF_HUB_OFFLINE".into(), "1".into()));
        worker.load_within = Duration::from_secs(150);
        let child = load(&worker).map_err(|error| {
            if error.kind() == std::io::ErrorKind::TimedOut {
                Deaf::Loading
            } else {
                Deaf::Failed(format!("could not load transcription model {id}: {error}"))
            }
        })?;
        let loaded = Self::new(worker);
        *locked(&loaded.state) = State::Up(child);
        Ok(loaded)
    }

    /// Starts the worker now, so that its model is loaded when the first clip comes.
    pub fn warm(&self) {
        let mut state = locked(&self.state);
        if matches!(*state, State::Down) {
            self.start(&mut state);
        }
    }

    /// Has a worker loaded on a thread of its own. The state says `Loading` until it is.
    fn start(&self, state: &mut State) {
        *state = State::Loading;
        let (worker, shared) = (self.worker.clone(), Arc::clone(&self.state));
        std::thread::spawn(move || {
            let loaded = load(&worker);
            let mut state = locked(&shared);
            *state = match loaded {
                Ok(running) => State::Up(running),
                Err(why) => State::Broken {
                    why: why.to_string(),
                    at: Instant::now(),
                },
            };
        });
    }

    /// The worker, for one clip. It is waited for when it hears another, as long as `wait`.
    fn take(&self, wait: Duration) -> Result<Running, Deaf> {
        let until = Instant::now() + wait;
        loop {
            {
                let mut state = locked(&self.state);
                match std::mem::replace(&mut *state, State::Hearing) {
                    State::Up(running) => return Ok(running),
                    State::Hearing => {}
                    State::Loading => {
                        *state = State::Loading;
                        return Err(Deaf::Loading);
                    }
                    State::Down => {
                        self.start(&mut state);
                        return Err(Deaf::Loading);
                    }
                    State::Broken { why, at } => {
                        if at.elapsed() >= TRIED_AGAIN_AFTER {
                            self.start(&mut state);
                            return Err(Deaf::Loading);
                        }
                        *state = State::Broken {
                            why: why.clone(),
                            at,
                        };
                        return Err(Deaf::Failed(why));
                    }
                }
            }
            if Instant::now() >= until {
                return Err(Deaf::Busy);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    /// Writes a clip where the worker reads it, for the owner's eyes only.
    fn written(&self, clip: Clip<'_>) -> std::io::Result<PathBuf> {
        let number = self.clips.fetch_add(1, Ordering::Relaxed);
        let name = format!(
            "clarp-heard-{}-{number}{}",
            std::process::id(),
            suffix(clip.content_type)
        );
        let path = self.worker.scratch.join(name);
        std::fs::create_dir_all(&self.worker.scratch)?;
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        file.write_all(clip.audio)?;
        file.flush()?;
        Ok(path)
    }

    /// Only an already-loaded, idle worker may probe; probing never starts a model.
    pub fn probe(&self, clip: Clip<'_>) -> Option<f64> {
        let mut running = {
            let mut state = locked(&self.state);
            if !matches!(*state, State::Up(_)) {
                return None;
            }
            match std::mem::replace(&mut *state, State::Hearing) {
                State::Up(r) => r,
                _ => unreachable!(),
            }
        };
        let Ok(path) = self.written(clip) else {
            *locked(&self.state) = State::Up(running);
            return None;
        };
        let id = format!("p{}", self.clips.load(Ordering::Relaxed));
        let request = json!({"op":"probe","id":id,"path":path});
        let answer = running
            .say(&request)
            .map_err(|_| Unheard::Gone(ENDED))
            .and_then(|()| running.reply(&id, Duration::from_secs(6)));
        let _ = std::fs::remove_file(path);
        if matches!(answer, Err(Unheard::Gone(_))) {
            drop(running);
            *locked(&self.state) = State::Down;
            return None;
        }
        *locked(&self.state) = State::Up(running);
        answer
            .ok()
            .and_then(|v| v["seconds"].as_f64())
            .filter(|s| s.is_finite() && *s > 0.)
    }

    /// Asks the worker for one clip.
    fn ask(
        &self,
        running: &mut Running,
        path: &Path,
        asked: Asked<'_>,
        observed: &mut Option<String>,
    ) -> Result<Vec<Segment>, Unheard> {
        let id = format!("c{}", self.clips.load(Ordering::Relaxed));
        let mut request = json!({
            "op": "transcribe",
            "id": id,
            "path": path.to_string_lossy(),
            "prompt": asked.listen_for,
            "beam_size": self.worker.beam_size.max(1),
        });
        if let Some(language) = asked.language.filter(|language| !language.is_empty()) {
            request["language"] = json!(language);
        }
        if running.say(&request).is_err() {
            return Err(Unheard::Gone(ENDED));
        }
        *observed = Some(
            asked
                .language
                .or(self.worker.language.as_deref())
                .unwrap_or("auto")
                .to_string(),
        );
        let said = running.reply(&id, self.worker.hear_within)?;
        let pieces = said["segments"].as_array().cloned().unwrap_or_default();
        Ok(pieces
            .iter()
            .map(|piece| Segment {
                text: piece["text"].as_str().unwrap_or("").to_string(),
                no_speech_prob: piece["no_speech_prob"].as_f64().unwrap_or(0.0),
            })
            .collect())
    }
}

impl Listener for Whisper {
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf> {
        self.hear_observed(clip, asked, &mut None)
    }
    fn hear_observed(
        &self,
        clip: Clip<'_>,
        asked: Asked<'_>,
        observed: &mut Option<String>,
    ) -> Result<Heard, Deaf> {
        let mut running = self.take(asked.wait)?;
        let started = Instant::now();
        let path = match self.written(clip) {
            Ok(path) => path,
            Err(error) => {
                *locked(&self.state) = State::Up(running);
                return Err(Deaf::Failed(format!(
                    "the clip could not be written for the transcription worker: {error}"
                )));
            }
        };
        let answered = self.ask(&mut running, &path, asked, observed);
        let _ = std::fs::remove_file(&path);
        match answered {
            Ok(segments) => {
                *locked(&self.state) = State::Up(running);
                Ok(Heard {
                    segments,
                    seconds: started.elapsed().as_secs_f64(),
                })
            }
            Err(Unheard::Said(why)) => {
                *locked(&self.state) = State::Up(running);
                Err(Deaf::Failed(why))
            }
            Err(Unheard::Gone(why)) => {
                // Gone or stuck: it is ended, and the next clip starts another.
                drop(running);
                *locked(&self.state) = State::Down;
                Err(Deaf::Failed(why.to_string()))
            }
        }
    }

    fn ready(&self) -> bool {
        matches!(*locked(&self.state), State::Up(_) | State::Hearing)
    }
}

impl Drop for Whisper {
    fn drop(&mut self) {
        // The worker goes with the engine. One that is still loading ends when it has.
        *locked(&self.state) = State::Down;
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::AtomicU32;

    use super::*;

    static PLACES: AtomicU32 = AtomicU32::new(0);

    #[test]
    fn speech_child_drops_the_host_token() {
        if crate::run_with_fixture_host_token("whisper::tests::speech_child_drops_the_host_token") {
            return;
        }
        let script = "test -z \"${CLARP_HOST_TOKEN+x}\" || exit 9";
        let mut worker = Worker::new("/bin/sh", "synthetic");
        worker.args = vec![
            "-c".into(),
            format!("{script}; read -r line; echo '{{\"event\":\"ready\"}}'; read -r line"),
        ];
        worker.load_within = Duration::from_secs(3);
        assert!(load(&worker).is_ok());
    }

    #[test]
    fn a_model_is_given_half_the_cores_four_at_most_and_one_at_least() {
        let given: Vec<u32> = [0, 1, 2, 3, 4, 6, 8, 9, 64]
            .into_iter()
            .map(threads_for)
            .collect();
        assert_eq!(given, [1, 1, 1, 1, 2, 3, 4, 4, 4]);
    }

    /// A worker that hears nothing and answers as the real one does: how many bytes the clip
    /// has, and a piece it is not sure was speech. What it is prompted with says how it
    /// misbehaves. (A line with a backslash in it is written with `printf`: what `echo`
    /// makes of one differs from shell to shell.)
    const STAND_IN: &str = r#"#!/bin/sh
while IFS= read -r line; do
  case "$line" in
    *'"op":"load"'*)
      case "$line" in
        *'"model_source":"broken"'*|*'/broken"'*)
          printf '%s\n' '{"event":"load_error","error":"no such model\n  File \"worker.py\", line 1"}'
          exit 1 ;;
      esac
      echo 'a line that is no JSON'
      echo '{"event":"ready"}' ;;
    *'"op":"transcribe"'*)
      id=${line#*\"id\":\"}; id=${id%%\"*}
      path=${line#*\"path\":\"}; path=${path%%\"*}
      case "$line" in
        *'"prompt":"fail"'*) echo "{\"id\":\"$id\",\"error\":\"synthetic failure\"}" ;;
        *'"prompt":"die"'*) exit 3 ;;
        *)
          case "$line" in *'"prompt":"slow"'*) sleep 1 ;; esac
          bytes=$(wc -c < "$path" | tr -d ' ')
          language=none
          case "$line" in *'"language":"nb"'*) language=nb ;; esac
          echo "{\"id\":\"stale\",\"segments\":[]}"
          echo "{\"id\":\"$id\",\"segments\":[{\"text\":\" heard $bytes bytes in $language.\",\"no_speech_prob\":0.1},{\"text\":\" noise\",\"no_speech_prob\":0.9}]}" ;;
      esac ;;
    *'"op":"quit"'*) exit 0 ;;
  esac
done
"#;

    struct Place {
        dir: PathBuf,
    }

    impl Place {
        fn new() -> Self {
            let number = PLACES.fetch_add(1, Ordering::SeqCst);
            let dir = std::env::temp_dir().join(format!(
                "clarp-whisper-test-{}-{number}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(dir.join("clips")).unwrap();
            let script = dir.join("worker.sh");
            std::fs::write(&script, STAND_IN).unwrap();
            std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
            Self { dir }
        }

        fn worker(&self, model: &str) -> Worker {
            Worker {
                args: vec![self.dir.join("worker.sh").to_string_lossy().to_string()],
                scratch: self.dir.join("clips"),
                load_within: Duration::from_secs(30),
                hear_within: Duration::from_secs(30),
                ..Worker::new("sh", model)
            }
        }

        fn clips(&self) -> usize {
            std::fs::read_dir(self.dir.join("clips")).unwrap().count()
        }
    }

    impl Drop for Place {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn clip() -> Clip<'static> {
        Clip {
            audio: b"made-up audio",
            content_type: "audio/mp4",
        }
    }

    fn asked(listen_for: &str, wait_ms: u64) -> Asked<'_> {
        Asked {
            listen_for,
            language: None,
            wait: Duration::from_millis(wait_ms),
        }
    }

    fn until(what: &str, mut holds: impl FnMut() -> bool) {
        let end = Instant::now() + Duration::from_secs(20);
        while !holds() {
            assert!(Instant::now() < end, "waited in vain for {what}");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn an_installed_variant_loads_before_its_first_clip() {
        let place = Place::new();
        let default = Whisper::new(place.worker("small.en"));
        let variant = default
            .installed_variant("faster-whisper:medium", &place.dir, None)
            .unwrap();
        assert!(variant.ready());
        assert!(!default.ready());
        let mut handed = None;
        let heard = variant
            .hear_observed(clip(), asked("", 0), &mut handed)
            .unwrap();
        assert_eq!(handed.as_deref(), Some("auto"));
        assert!(heard.segments[0].text.contains("13 bytes"));
        let broken =
            default.installed_variant("faster-whisper:broken", &place.dir.join("broken"), None);
        assert!(matches!(broken, Err(Deaf::Failed(why)) if why.contains("no such model")));
    }

    #[test]
    fn a_worker_loads_its_model_and_then_hears_clips() {
        let place = Place::new();
        let whisper = Whisper::new(place.worker("small.en"));
        assert!(!whisper.ready());
        // The first clip starts the worker, and is told that it is loading.
        assert_eq!(whisper.hear(clip(), asked("", 0)), Err(Deaf::Loading));
        until("the model to be loaded", || whisper.ready());
        let heard = whisper.hear(clip(), asked("Clarp, Maximus", 0)).unwrap();
        assert_eq!(
            heard.segments,
            [
                Segment {
                    text: " heard 13 bytes in none.".to_string(),
                    no_speech_prob: 0.1
                },
                Segment {
                    text: " noise".to_string(),
                    no_speech_prob: 0.9
                },
            ]
        );
        assert!(heard.seconds >= 0.0);
        assert_eq!(place.clips(), 0, "a clip that was heard is not kept");
        // A clip can say which language is spoken in it.
        let other = Asked {
            language: Some("nb"),
            ..asked("", 0)
        };
        let heard = whisper.hear(clip(), other).unwrap();
        assert_eq!(heard.segments[0].text, " heard 13 bytes in nb.");
        assert!(whisper.ready());
    }

    #[test]
    fn a_worker_started_ahead_is_ready_when_the_first_clip_comes() {
        let place = Place::new();
        let whisper = Whisper::new(place.worker("small.en"));
        whisper.warm();
        until("the model to be loaded", || whisper.ready());
        assert!(whisper.hear(clip(), asked("", 0)).is_ok());
    }

    #[test]
    fn one_clip_is_heard_at_a_time_and_the_next_waits_or_is_told_busy() {
        let place = Place::new();
        let whisper = Arc::new(Whisper::new(place.worker("small.en")));
        whisper.warm();
        until("the model to be loaded", || whisper.ready());
        let slow = {
            let whisper = Arc::clone(&whisper);
            std::thread::spawn(move || whisper.hear(clip(), asked("slow", 0)))
        };
        until("the slow clip to be taken", || {
            matches!(*locked(&whisper.state), State::Hearing)
        });
        // Who does not wait is told it is busy; who waits long enough is heard after.
        assert_eq!(whisper.hear(clip(), asked("", 0)), Err(Deaf::Busy));
        assert!(whisper.hear(clip(), asked("", 20_000)).is_ok());
        assert!(slow.join().unwrap().is_ok());
        assert_eq!(place.clips(), 0);
    }

    #[test]
    fn a_worker_that_says_why_stays_and_one_that_dies_is_started_again() {
        let place = Place::new();
        let whisper = Whisper::new(place.worker("small.en"));
        whisper.warm();
        until("the model to be loaded", || whisper.ready());
        assert_eq!(
            whisper.hear(clip(), asked("fail", 0)),
            Err(Deaf::Failed("synthetic failure".to_string()))
        );
        assert!(whisper.ready(), "it said why: it is as good as before");
        assert!(whisper.hear(clip(), asked("", 0)).is_ok());
        // It falls over in the middle of a clip.
        assert_eq!(
            whisper.hear(clip(), asked("die", 0)),
            Err(Deaf::Failed(ENDED.to_string()))
        );
        assert!(!whisper.ready());
        assert_eq!(place.clips(), 0, "the clip it died over is removed too");
        // The next clip starts another worker.
        assert_eq!(whisper.hear(clip(), asked("", 0)), Err(Deaf::Loading));
        until("the model to be loaded again", || whisper.ready());
        assert!(whisper.hear(clip(), asked("", 0)).is_ok());
    }

    #[test]
    fn a_model_that_does_not_load_is_said_so_and_a_worker_that_is_not_there_too() {
        let place = Place::new();
        let whisper = Whisper::new(place.worker("broken"));
        whisper.warm();
        until("the load to fail", || {
            matches!(*locked(&whisper.state), State::Broken { .. })
        });
        assert_eq!(
            whisper.hear(clip(), asked("", 0)),
            Err(Deaf::Failed(
                "the transcription model did not load: no such model".to_string()
            ))
        );
        assert!(!whisper.ready());

        let nowhere = Worker::new(place.dir.join("no-such-program"), "small.en");
        let whisper = Whisper::new(nowhere);
        whisper.warm();
        until("the start to fail", || {
            matches!(*locked(&whisper.state), State::Broken { .. })
        });
        let Err(Deaf::Failed(why)) = whisper.hear(clip(), asked("", 0)) else {
            panic!("a worker that is not there hears nothing");
        };
        assert!(
            why.starts_with("the transcription worker could not be started"),
            "{why}"
        );
    }
}
