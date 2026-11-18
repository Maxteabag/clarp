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
//! `call_model`), except that there the question is on the command line.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use clarp_backend::{Launch, OwnHome, Question, Reading};
use tokio::io::AsyncWriteExt as _;

/// The least time a question is given.
const LEAST: Duration = Duration::from_millis(250);
/// How much of what a tool said when it failed is told, in signs.
const SAID_MOST: usize = 1_000;

/// Why a question was not answered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Unanswered {
    /// The Host has no tool of this name that can be asked one question.
    NoSuchTool(String),
    /// The tool's program could not be started: what the system said.
    NotStarted(String),
    /// It did not answer in its time, and was ended.
    TimedOut,
    /// It ended badly: what it said, cut short.
    Failed(String),
    /// It ended well, and there is no answer in what it printed.
    NoAnswer(String),
}

impl Unanswered {
    /// In words, for whoever is told why.
    pub fn words(&self) -> String {
        match self {
            Self::NoSuchTool(name) => format!("unsupported provider: {name}"),
            Self::NotStarted(why) | Self::Failed(why) | Self::NoAnswer(why) => why.clone(),
            Self::TimedOut => "the model did not answer in time".to_string(),
        }
    }
}

/// Asks the tool of this name one question, in `cwd`, and waits for its answer for
/// `within` at most.
pub async fn ask(
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
    let apart = match tool.own_home() {
        Some(given) => {
            let place = a_place("asking")?;
            let home = own_home(&place, &given);
            launch.env.push(("HOME".to_string(), home?));
            Some(place)
        }
        None => None,
    };
    let printed = run(&launch, cwd, within).await;
    if let Some(place) = apart {
        let _ = std::fs::remove_dir_all(&place);
    }
    tool.answered(&printed?).map_err(Unanswered::NoAnswer)
}

/// A directory of the Host's own for one question, that nobody else may look into.
fn a_place(what: &str) -> Result<PathBuf, Unanswered> {
    use std::os::unix::fs::DirBuilderExt as _;
    let place = std::env::temp_dir().join(format!("clarp-{what}-{}", crate::ids::hex(8)));
    let made = std::fs::DirBuilder::new().mode(0o700).create(&place);
    made.map_err(|error| Unanswered::NotStarted(format!("{}: {error}", place.display())))?;
    Ok(place)
}

/// Makes a tool's own home in `place` (`home`), with what it is given of its user's: the
/// login linked, the settings copied, and beside it an empty directory to work in
/// (`work`). What its user has not got is left out. Answers the home.
fn own_home(place: &Path, given: &OwnHome) -> Result<String, Unanswered> {
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
    if matches!(backend,"grok" | "muse") { return Err(none()); }
    let place = a_place("reading")?;
    // A tool that answers in a home of its own has it here, and works beside it: what it
    // reads for is not where its login is linked.
    let (home, works_in) = match tool.own_home().map(|given| own_home(&place, &given)) {
        Some(Ok(home)) => (Some(home), place.join("work")),
        Some(Err(why)) => {
            let _ = std::fs::remove_dir_all(&place);
            return Err(why);
        }
        None => (None, place.clone()),
    };
    let at_home = |launch: &mut Launch| {
        if let Some(home) = &home {
            launch.env.push(("HOME".to_string(), home.clone()));
        }
    };
    let printed = match tool.reads(reading) {
        Some(apart) => {
            let here = place.to_string_lossy();
            let mut launch = apart.launch;
            for arg in &mut launch.args {
                *arg = arg.replace("{dir}", &here);
            }
            at_home(&mut launch);
            let mut given = apart.files.iter();
            match given.try_for_each(|(name, holds)| std::fs::write(place.join(name), holds)) {
                Ok(()) => run_with(&launch, &works_in, within, Some(&apart.keeps)).await,
                Err(error) => Err(Unanswered::NotStarted(error.to_string())),
            }
        }
        None => {
            let prompt = format!(
                "{}\n{}\nReturn only the assistant answer as one JSON object matching this \
                 exact schema: {}",
                reading.instructions, reading.asked, reading.schema
            );
            let question = Question {
                prompt,
                model: reading.model.clone(),
                effort: reading.effort.clone(),
            };
            match tool.once(&question) {
                Some(mut launch) => {
                    at_home(&mut launch);
                    run_with(&launch, &works_in, within, None).await
                }
                None => Err(none()),
            }
        }
    };
    let _ = std::fs::remove_dir_all(&place);
    tool.answered(&printed?).map_err(Unanswered::NoAnswer)
}

/// Runs a tool once, as it says it is started for one question, and answers what it
/// printed.
async fn run(launch: &Launch, cwd: &Path, within: Duration) -> Result<String, Unanswered> {
    run_with(launch, cwd, within, None).await
}

/// The same; `keeps`: the only variables of the Host's environment the tool is left with.
async fn run_with(
    launch: &Launch,
    cwd: &Path,
    within: Duration,
    keeps: Option<&[&str]>,
) -> Result<String, Unanswered> {
    let mut command = tokio::process::Command::new(&launch.program);
    if let Some(keeps) = keeps {
        command.env_clear();
        for name in keeps {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
    }
    command
        .args(&launch.args)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    // The Host's own secrets stay with the Host, as for a turn.
    for name in crate::OWN_SECRETS {
        command.env_remove(name);
    }
    for (name, value) in &launch.env {
        command.env(name, value);
    }
    let mut child = command
        .spawn()
        .map_err(|error| Unanswered::NotStarted(format!("{}: {error}", launch.program)))?;
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
        Ok(Err(error)) => return Err(Unanswered::Failed(error.to_string())),
        Ok(Ok(done)) => done,
    };
    let printed = String::from_utf8_lossy(&done.stdout);
    if !done.status.success() {
        let complained = String::from_utf8_lossy(&done.stderr);
        let said = [complained.trim(), printed.trim()];
        let said = said.into_iter().find(|said| !said.is_empty());
        let said = match said {
            Some(said) => said.chars().take(SAID_MOST).collect(),
            None => format!("{} ended with {}", launch.program, done.status),
        };
        return Err(Unanswered::Failed(said));
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
        let failed = Unanswered::Failed("no login".to_string());
        assert_eq!(failed.words(), "no login");
    }

    #[tokio::test]
    async fn a_tool_nobody_has_and_one_that_cannot_be_asked_once_answer_nothing() {
        let question = Question::default();
        let here = std::env::temp_dir();
        let short = Duration::from_millis(300);
        assert_eq!(
            ask("abacus", &question, &here, short).await,
            Err(Unanswered::NoSuchTool("abacus".to_string()))
        );
        // The stand-in tool of the tests has turns and no single questions.
        assert_eq!(
            ask("scripted", &question, &here, short).await,
            Err(Unanswered::NoSuchTool("scripted".to_string()))
        );
    }

    /// A tool for one question that is a line of shell.
    fn shell(line: &str, question: &str) -> Launch {
        Launch {
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
            run(&echo, &here, long).await,
            Ok("a question: Whom is this for?".to_string())
        );
        // It runs where it is told to.
        let place = run(&shell("pwd -P", ""), &here, long).await.unwrap();
        assert_eq!(
            std::path::Path::new(place.trim()),
            here.canonicalize().unwrap()
        );
        // One that ends badly says why: what it complained of, else what it printed, else
        // how it ended.
        let complains = shell("echo printed; echo no login >&2; exit 3", "");
        assert_eq!(
            run(&complains, &here, long).await,
            Err(Unanswered::Failed("no login".to_string()))
        );
        let prints = shell("echo only printed; exit 3", "");
        assert_eq!(
            run(&prints, &here, long).await,
            Err(Unanswered::Failed("only printed".to_string()))
        );
        let Err(Unanswered::Failed(silent)) = run(&shell("exit 3", ""), &here, long).await else {
            panic!("a tool that ended badly answered");
        };
        assert!(silent.starts_with("sh ended with "), "{silent}");
        // One that does not answer in its time is given up, at that time.
        let began = std::time::Instant::now();
        let slow = run(&shell("sleep 20", ""), &here, Duration::from_millis(300)).await;
        assert_eq!(slow, Err(Unanswered::TimedOut));
        assert!(began.elapsed() < Duration::from_secs(5));
        // One that is not there to run is not started.
        let missing = Launch {
            program: "/nowhere/no-such-tool".to_string(),
            ..shell("", "")
        };
        let Err(Unanswered::NotStarted(why)) = run(&missing, &here, long).await else {
            panic!("a tool that is not there ran");
        };
        assert!(why.starts_with("/nowhere/no-such-tool: "), "{why}");
    }
}
