//! One question to a tool's model.
//!
//! A janitor that answers one request (the one that finds whom a spoken message is for,
//! the one that explains the use of a tool) asks a model once and is done: no agent, no
//! conversation, no turn. The tool says how it is started for that and where its answer is
//! in what it prints ([`clarp_backend::Backend::once`], `answered`); here it is run: where
//! the Host's agents work by default, with its question on standard input, without the
//! Host's own secrets, and for no longer than it is given. A question that is not answered
//! in time is ended. Unlike a turn it is the Host's own child: nobody would read its
//! answer after the Host is gone.
//!
//! This is how the Python Host asks a tool one question (`lib/orchestrator_model.py`,
//! `call_model`), including its choice of stdin, a private file or a bounded argument.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use clarp_backend::{Launch, OwnHome, Question, Reading};
use tokio::io::AsyncWriteExt as _;

/// The least time a question is given.
const LEAST: Duration = Duration::from_millis(250);
/// Why a question was not answered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Unanswered {
    /// The Host has no tool of this name that can be asked one question.
    NoSuchTool(String),
    /// The tool's program could not be started: what the system said.
    NotStarted(String),
    /// It did not answer in its time, and was ended.
    TimedOut,
    RoutingTimeout(String),
    PromptNotEncodable(String),
    PromptTooLarge(String),
    /// It ended badly: its facts, and of its words only what holds none of the question
    /// (`clarp_kernel::failures::cli_failure`).
    Failed(clarp_kernel::failures::CliFailure),
    /// It ended well, and there is no answer in what it printed.
    NoAnswer(String),
}

impl Unanswered {
    /// In words, for whoever is told why.
    pub fn words(&self) -> String {
        match self {
            Self::NoSuchTool(name) => format!("unsupported provider: {name}"),
            Self::RoutingTimeout(why)
            | Self::PromptNotEncodable(why)
            | Self::PromptTooLarge(why)
            | Self::NotStarted(why)
            | Self::NoAnswer(why) => why.clone(),
            Self::Failed(failed) => failed.line.clone(),
            Self::TimedOut => "the model did not answer in time".to_string(),
        }
    }
}

/// The scratch home is removed when an in-flight model question is cancelled.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct ChildGroup(nix::unistd::Pid);

impl Drop for ChildGroup {
    fn drop(&mut self) {
        let _ = nix::sys::signal::killpg(self.0, nix::sys::signal::Signal::SIGKILL);
    }
}

fn prepare(
    root: &Path,
    tool: &str,
    launch: &mut Launch,
    prompt: &str,
) -> Result<Option<Scratch>, Unanswered> {
    use clarp_kernel::janitors::routing_input::{self, Input};
    use std::io::Write as _;
    use std::os::unix::fs::OpenOptionsExt as _;
    // Claude and Codex adapters already supply stdin. Their final argument is a
    // genuine option even if it happens to equal the prompt.
    if !launch.stdin.is_empty() {
        return Ok(None);
    }
    let raw = routing_input::prompt_bytes(tool, prompt.chars().map(u32::from))
        .map_err(Unanswered::PromptNotEncodable)?;
    match routing_input::prepare(tool, &mut launch.args, prompt)
        .map_err(Unanswered::PromptTooLarge)?
    {
        Input::Arguments => Ok(None),
        Input::Stdin => {
            launch.stdin = raw;
            Ok(None)
        }
        Input::File => {
            let root = Scratch(private_place(root, "routing")?);
            let path = root.0.join("prompt.txt");
            let failed = |err: std::io::Error| Unanswered::NotStarted(err.to_string());
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
                .map_err(failed)?;
            file.write_all(&raw).map_err(failed)?;
            launch.args.push(path.to_string_lossy().into_owned());
            Ok(Some(root))
        }
    }
}

/// Asks the tool of this name one question, in `cwd`, and waits for its answer for
/// `within` at most.
pub async fn ask(
    routing_root: &Path,
    backend: &str,
    question: &Question,
    cwd: &Path,
    within: Duration,
) -> Result<String, Unanswered> {
    let none = || Unanswered::NoSuchTool(backend.to_string());
    let tool = clarp_backend::by_id(backend).ok_or_else(none)?;
    let mut launch = tool.once(question).ok_or_else(none)?;
    // A tool that answers in a home of its own is given one for this question, and it is
    // taken away afterwards with whatever the tool kept of the question.
    let _apart = match tool.own_home() {
        Some(given) => {
            let place = Scratch(a_place("asking")?);
            let home = own_home(&place.0, &given);
            launch.env.push(("HOME".to_string(), home?));
            Some(place)
        }
        None => None,
    };
    let _prompt_file = prepare(routing_root, backend, &mut launch, &question.prompt)?;
    let printed = run(&launch, cwd, within, (backend, &question.prompt)).await;
    tool.answered(&routing_result(backend, within, printed)?)
        .map_err(Unanswered::NoAnswer)
}

fn routing_result(
    tool: &str,
    within: Duration,
    printed: Result<String, Unanswered>,
) -> Result<String, Unanswered> {
    printed.map_err(|e| match e {
        Unanswered::TimedOut => Unanswered::RoutingTimeout(
            clarp_kernel::janitors::routing_input::timeout(tool, within.max(LEAST).as_secs_f64()),
        ),
        other => other,
    })
}

/// Sweep only this Host's private prompt directories. symlink_metadata checks the
/// entry itself; remove_dir_all on Unix does not follow links inside the tree.
pub fn sweep_routing(root: &Path, now: std::time::SystemTime) {
    use std::os::unix::fs::MetadataExt;
    if !std::fs::symlink_metadata(root).is_ok_and(|m| m.is_dir()) {
        return;
    }
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(suffix) = name.to_str().and_then(|s| s.strip_prefix("clarp-routing-")) else {
            continue;
        };
        if suffix.len() != 16 || !suffix.bytes().all(|b| b.is_ascii_hexdigit()) {
            continue;
        }
        let path = entry.path();
        let Ok(meta) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        if meta.is_dir()
            && meta.uid() == nix::unistd::getuid().as_raw()
            && meta
                .modified()
                .ok()
                .and_then(|m| now.duration_since(m).ok())
                .is_some_and(|age| age > Duration::from_secs(3600))
        {
            // One that cannot be removed is left unsaid, as the Python Host leaves it
            // (`shutil.rmtree(..., ignore_errors=True)`): the next start tries again.
            let _ = std::fs::remove_dir_all(path);
        }
    }
}

/// A directory of the Host's own for one question, that nobody else may look into.
pub(crate) fn a_place(what: &str) -> Result<PathBuf, Unanswered> {
    private_place(&std::env::temp_dir(), what)
}

fn private_place(parent: &Path, what: &str) -> Result<PathBuf, Unanswered> {
    use std::os::unix::fs::DirBuilderExt as _;
    let place = std::path::absolute(parent)
        .map_err(|e| Unanswered::NotStarted(e.to_string()))?
        .join(format!("clarp-{what}-{}", crate::ids::hex(8)));
    let made = std::fs::DirBuilder::new().mode(0o700).create(&place);
    made.map_err(|error| Unanswered::NotStarted(format!("{}: {error}", place.display())))?;
    Ok(place)
}

/// Makes a tool's own home in `place` (`home`), with what it is given of its user's: the
/// login linked, the settings copied, and beside it an empty directory to work in
/// (`work`). What its user has not got is left out. Answers the home.
pub(crate) fn own_home(place: &Path, given: &OwnHome) -> Result<String, Unanswered> {
    let failed = |error: std::io::Error| Unanswered::NotStarted(error.to_string());
    let users = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
    let home = place.join("home");
    std::fs::create_dir_all(&home).map_err(failed)?;
    std::fs::create_dir_all(place.join("work")).map_err(failed)?;
    let both = given.linked.iter().map(|relative| (relative, true));
    for (relative, linked) in both.chain(given.copied.iter().map(|relative| (relative, false))) {
        let (theirs, ours) = (users.join(relative), home.join(relative));
        if !theirs.exists() {
            continue;
        }
        if let Some(parent) = ours.parent() {
            std::fs::create_dir_all(parent).map_err(failed)?;
        }
        if linked {
            std::os::unix::fs::symlink(&theirs, &ours).map_err(failed)?;
        } else {
            std::fs::copy(&theirs, &ours).map_err(failed)?;
        }
    }
    Ok(home.to_string_lossy().into_owned())
}

pub(crate) async fn read_on(
    app: &crate::edge::App,
    tool: &str,
    reading: &Reading,
    within: Duration,
) -> Result<String, Unanswered> {
    let _login = crate::pool::Service::use_login(app, tool)
        .await
        .map_err(|_| Unanswered::NotStarted("The login lock is unavailable".into()))?;
    read(tool, reading, within).await
}

/// Has a tool read something and answer in a shape it is given, for `within` at most:
/// what it reads is whatever stood in somebody's work, and it must not act on it. It is
/// started in a directory of its own, which is taken away afterwards, with nothing to act
/// with where the tool has such a way ([`clarp_backend::Backend::reads`]); a tool that has
/// none is asked as for any question, there, with the shape in words.
pub async fn read(
    backend: &str,
    reading: &Reading,
    within: Duration,
) -> Result<String, Unanswered> {
    let none = || Unanswered::NoSuchTool(backend.to_string());
    let tool = clarp_backend::by_id(backend).ok_or_else(none)?;
    // These tools auto-approve actions for agent turns. A scratch cwd alone cannot
    // make a janitor's reading of somebody else's text read-only.
    if matches!(backend, "grok" | "muse") {
        return Err(none());
    }
    let scratch = Scratch(a_place("reading")?);
    let place = &scratch.0;
    // A tool that answers in a home of its own has it here, and works beside it: what it
    // reads for is not where its login is linked.
    let (home, works_in) = match tool.own_home().map(|given| own_home(place, &given)) {
        Some(Ok(home)) => (Some(home), place.join("work")),
        Some(Err(why)) => {
            return Err(why);
        }
        None => (None, place.clone()),
    };
    let at_home = |launch: &mut Launch| {
        if let Some(home) = &home {
            launch.env.push(("HOME".to_string(), home.clone()));
        }
    };
    // What the tool is asked, as the reference's `json_call` asks it: what a failure's words
    // are held against, also where the tool reads its instructions from a file of its own.
    let prompt = format!(
        "{}\n{}\nReturn only the assistant answer as one JSON object matching this \
         exact schema: {}",
        reading.instructions, reading.asked, reading.schema
    );
    let printed = match tool.reads(reading) {
        Some(apart) => {
            let here = place.to_string_lossy();
            let mut launch = apart.launch;
            for arg in &mut launch.args {
                *arg = arg.replace("{dir}", &here);
            }
            at_home(&mut launch);
            clarp_kernel::janitors::routing_input::check(backend, &launch.args)
                .map_err(Unanswered::PromptTooLarge)?;
            let mut given = apart.files.iter();
            match given.try_for_each(|(name, holds)| std::fs::write(place.join(name), holds)) {
                Ok(()) => {
                    let asked = (backend, prompt.as_str());
                    run_with(&launch, &works_in, within, Some(&apart.keeps), asked).await
                }
                Err(error) => Err(Unanswered::NotStarted(error.to_string())),
            }
        }
        None => {
            let question = Question {
                prompt: prompt.clone(),
                model: reading.model.clone(),
                effort: reading.effort.clone(),
            };
            match tool.once(&question) {
                Some(mut launch) => {
                    at_home(&mut launch);
                    let _prompt_file = prepare(place, backend, &mut launch, &question.prompt)?;
                    run_with(
                        &launch,
                        &works_in,
                        within,
                        None,
                        (backend, &question.prompt),
                    )
                    .await
                }
                None => Err(none()),
            }
        }
    };
    tool.answered(&routing_result(backend, within, printed)?)
        .map_err(Unanswered::NoAnswer)
}

/// Runs a tool once, as it says it is started for one question, and answers what it
/// printed. `asked`: the tool's name and the question it was given, which a failure's words
/// are held against.
async fn run(
    launch: &Launch,
    cwd: &Path,
    within: Duration,
    asked: (&str, &str),
) -> Result<String, Unanswered> {
    run_with(launch, cwd, within, None, asked).await
}

/// The same; `keeps`: the only variables of the Host's environment the tool is left with.
async fn run_with(
    launch: &Launch,
    cwd: &Path,
    within: Duration,
    keeps: Option<&[&str]>,
    (backend, prompt): (&str, &str),
) -> Result<String, Unanswered> {
    let mut command = crate::child::one_off(&launch.program, keeps);
    command
        .process_group(0)
        .args(&launch.args)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (name, value) in &launch.env {
        command.env(name, value);
    }
    let mut child = command
        .spawn()
        .map_err(|error| Unanswered::NotStarted(format!("{}: {error}", launch.program)))?;
    let _group = child
        .id()
        .map(|pid| ChildGroup(nix::unistd::Pid::from_raw(pid as i32)));
    let asked = async {
        if let Some(mut stdin) = child.stdin.take() {
            // A tool that ends without reading its question says why itself.
            let _ = stdin.write_all(&launch.stdin).await;
            let _ = stdin.shutdown().await;
        }
        child.wait_with_output().await
    };
    // A question that is given up drops its tool, which ends it.
    let done = match tokio::time::timeout(within.max(LEAST), asked).await {
        Err(_) => return Err(Unanswered::TimedOut),
        Ok(Err(error)) => {
            let failed =
                clarp_kernel::failures::cli_failure(backend, &error.to_string(), prompt, None);
            return Err(Unanswered::Failed(failed));
        }
        Ok(Ok(done)) => done,
    };
    let printed = String::from_utf8_lossy(&done.stdout);
    if !done.status.success() {
        // What it complained of, else what it printed (`p.stderr or p.stdout`), held against
        // the question: its own words are kept only where they hold none of it. The status as
        // the reference reads it: a signal is its negative.
        use std::os::unix::process::ExitStatusExt as _;
        let complained = String::from_utf8_lossy(&done.stderr);
        let said = if complained.is_empty() {
            &printed
        } else {
            &complained
        };
        let status = done
            .status
            .code()
            .map(i64::from)
            .or_else(|| done.status.signal().map(|signal| -i64::from(signal)));
        let failed = clarp_kernel::failures::cli_failure(backend, said, prompt, status);
        return Err(Unanswered::Failed(failed));
    }
    Ok(printed.into_owned())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn why_a_question_was_not_answered_is_said_in_words() {
        let none = Unanswered::NoSuchTool("abacus".to_string());
        assert_eq!(none.words(), "unsupported provider: abacus");
        assert_eq!(
            Unanswered::TimedOut.words(),
            "the model did not answer in time"
        );
        let failed =
            clarp_kernel::failures::cli_failure("codex", "no login", "a question", Some(3));
        assert_eq!(Unanswered::Failed(failed).words(), "no login");
    }

    #[test]
    fn a_prompt_directory_stays_through_its_last_live_instant() {
        use std::time::{Duration, SystemTime};
        let root = Scratch(a_place("sweep-test").unwrap());
        let dir = root.0.join("clarp-routing-0000000000000000");
        std::fs::create_dir(&dir).unwrap();
        let written = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        std::fs::File::open(&dir)
            .unwrap()
            .set_modified(written)
            .unwrap();
        sweep_routing(&root.0, written + Duration::from_secs(3600));
        assert!(dir.exists());
        sweep_routing(
            &root.0,
            written + Duration::from_secs(3600) + Duration::from_nanos(1),
        );
        assert!(!dir.exists());
    }

    #[tokio::test]
    async fn a_tool_nobody_has_and_one_that_cannot_be_asked_once_answer_nothing() {
        let question = Question::default();
        let here = std::env::temp_dir();
        let short = Duration::from_millis(300);
        assert_eq!(
            ask(&here, "abacus", &question, &here, short).await,
            Err(Unanswered::NoSuchTool("abacus".to_string()))
        );
        // The stand-in tool of the tests has turns and no single questions.
        assert_eq!(
            ask(&here, "scripted", &question, &here, short).await,
            Err(Unanswered::NoSuchTool("scripted".to_string()))
        );
    }

    /// A tool for one question that is a line of shell.
    fn shell(line: &str, question: &str) -> Launch {
        Launch {
            prompt_as_file: false,
            before_stop: None,
            program: "sh".to_string(),
            args: vec!["-c".to_string(), line.to_string()],
            env: vec![("ASKED_AS".to_string(), "a question".to_string())],
            stdin: question.as_bytes().to_vec(),
        }
    }

    #[tokio::test]
    async fn a_tool_is_run_once_with_its_question_and_for_no_longer_than_it_is_given() {
        let here = std::env::temp_dir();
        let long = Duration::from_secs(20);
        // It reads its question on standard input and is given what its launch names.
        let echo = shell("printf '%s: ' \"$ASKED_AS\"; cat", "Whom is this for?");
        assert_eq!(
            run(&echo, &here, long, ("sh", "Whom is this for?")).await,
            Ok("a question: Whom is this for?".to_string())
        );
        // It runs where it is told to.
        let place = run(&shell("pwd -P", ""), &here, long, ("sh", ""))
            .await
            .unwrap();
        assert_eq!(
            std::path::Path::new(place.trim()),
            here.canonicalize().unwrap()
        );
        // One that ends badly says why: what it complained of, else what it printed, else
        // how it ended; and never the question it was asked.
        let at = here.as_path();
        let words = |asked: &'static str, line: &str| {
            let said = shell(line, asked);
            async move {
                let Err(Unanswered::Failed(failed)) = run(&said, at, long, ("sh", asked)).await
                else {
                    panic!("a tool that ended badly answered");
                };
                failed
            }
        };
        let complains = words("", "echo printed; echo no login >&2; exit 3").await;
        assert_eq!(
            (complains.line.as_str(), complains.status),
            ("no login", Some(3))
        );
        let prints = words("", "echo only printed; exit 3").await;
        assert_eq!(prints.line, "only printed");
        let silent = words("", "exit 3").await;
        assert_eq!(silent.line, "sh exited with status 3 (runner_exit)");
        let asked = "Please book a table for four at the harbour restaurant tonight.";
        let repeats = words(asked, "cat >&2; exit 2").await;
        assert_eq!(repeats.line, "sh exited with status 2 (runner_exit)");
        assert!(repeats.moves_on);
        let killed = words("", "kill -9 $$").await;
        assert_eq!(killed.status, Some(-9));
        // One that does not answer in its time is given up, at that time.
        let began = std::time::Instant::now();
        let slow = run(
            &shell("sleep 20", ""),
            &here,
            Duration::from_millis(300),
            ("sh", ""),
        )
        .await;
        assert_eq!(slow, Err(Unanswered::TimedOut));
        assert!(began.elapsed() < Duration::from_secs(5));
        // One that is not there to run is not started.
        let missing = Launch {
            before_stop: None,
            program: "/nowhere/no-such-tool".to_string(),
            ..shell("", "")
        };
        let Err(Unanswered::NotStarted(why)) = run(&missing, &here, long, ("sh", "")).await else {
            panic!("a tool that is not there ran");
        };
        assert!(why.starts_with("/nowhere/no-such-tool: "), "{why}");
    }
}
