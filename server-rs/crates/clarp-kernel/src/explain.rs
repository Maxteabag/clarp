//! What is said of a tool's use in plain words, without asking a model.
//!
//! Under a tool's row an app shows a sentence that says what the tool does ("Lists the
//! files in the project folder"). Most uses of tools are a handful of kinds: a directory
//! listed, a file read, a text searched for, the state of a repository asked. For those
//! there are templates: a use is read (the words of a shell command, the fields of a
//! named tool), and where it is surely one of those kinds, the template's sentence for
//! the level of detail its reader chose is filled with what the use names. Where it is
//! not surely one (several commands at once, a program that changes or sends something, a
//! script, a program nobody listed), nothing is said here, and why: the reason decides who
//! may be asked instead.
//!
//! Nothing that is read is ever run. A parameter is said only when it stands literally in
//! what the use shows and is of its kind (a path, a pattern, a host).
//!
//! The rules and the library of templates are the Python Host's
//! (`server/lib/tool_explanation_templates.py`, `tool_explanation_templates.json`), held by
//! `tests/fixtures/tool-explanation-vectors.json`.
//!
//! Beside it: [`read`] is how a use is read as it comes from a turn or an app (the bounded
//! fields, with secrets and what a heredoc only feeds withheld), and [`shapes`] is what
//! parts a call has and under what shape each is remembered.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::{portraits, python, tools};

pub mod read;
pub mod shapes;

/// The library: every template with its kind of action, its parameters and their kinds,
/// and its sentences by level. The Python Host's own file, copied by
/// `tools/tool-explanation-vectors.py`.
static LIBRARY: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("explain-templates.json")).unwrap_or_default()
});

/// How long the name of a use can be when an app could show only its beginning: a shell
/// command that long, given as a name, is taken as cut off.
pub const LABEL_CLIP: usize = 80;
/// Where a heredoc stood in a command whose heredocs were taken out.
pub const HEREDOC: &str = "\u{3}";
/// How a heredoc reads once what it holds is withheld.
const WITHHELD_HEREDOC: &str = "<<'EOF'";
const WITHHELD_BODY: &str = "\n\u{2026}\nEOF";

const SEARCH: [&str; 6] = ["rg", "grep", "egrep", "fgrep", "ag", "ack"];
const READ: [&str; 7] = ["cat", "head", "tail", "nl", "less", "more", "bat"];
const SCRIPT: [&str; 12] = [
    "python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "deno", "bun", "tsx",
    "ts-node",
];
const PRIVILEGED: [&str; 4] = ["sudo", "doas", "su", "pkexec"];
const MUTATING: [&str; 43] = [
    "shred",
    "dd",
    "mkfs",
    "truncate",
    "chmod",
    "chown",
    "chgrp",
    "mv",
    "cp",
    "ln",
    "kill",
    "pkill",
    "killall",
    "systemctl",
    "docker",
    "podman",
    "kubectl",
    "reboot",
    "shutdown",
    "tee",
    "xargs",
    "install",
    "rsync",
    "scp",
    "ssh",
    "npm",
    "pnpm",
    "yarn",
    "pip",
    "pip3",
    "uv",
    "cargo",
    "make",
    "apt",
    "pacman",
    "brew",
    "eval",
    "exec",
    "env",
    "nohup",
    "watch",
    "crontab",
    "git-lfs",
];
const VALUE_FLAGS: [&str; 18] = [
    "-g",
    "--glob",
    "-t",
    "--type",
    "-T",
    "--type-not",
    "-A",
    "-B",
    "-C",
    "-m",
    "--max-count",
    "--max-depth",
    "-d",
    "--include",
    "--exclude",
    "--exclude-dir",
    "-M",
    "--max-columns",
];
/// What a shell does itself: it sets the shell's state, tries a condition or prints a
/// text. None of it reads, lists or searches files.
const BUILTINS: [&str; 37] = [
    "export", "unset", "set", "source", ".", "[", "test", "echo", "printf", "read", "sleep",
    "wait", "trap", "exit", "return", "local", "declare", "typeset", "readonly", "alias",
    "unalias", "shift", "umask", "ulimit", "type", "command", "hash", "jobs", "fg", "bg", "disown",
    "builtin", "let", "cd", "pushd", "popd", "dirs",
];
const WRAPPERS: [&str; 8] = [
    "env", "nice", "ionice", "nohup", "time", "timeout", "stdbuf", "sudo",
];

/// A pattern as Python reads it: what it calls blank (`\s`) takes the four separators
/// below the space too.
fn pattern(text: &str) -> Regex {
    let text = text
        .replace(r"\s", r"[\s\x1c-\x1f]")
        .replace(r"\S", r"[^\s\x1c-\x1f]");
    // Every pattern is written out in this file: one that does not compile is a mistake
    // here, which the tests of this module would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(&text).expect("a pattern of this module compiles")
}

static NAME: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$"));
static PROJECT: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$"));
static HOST: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"));
static UNSAFE: LazyLock<Regex> = LazyLock::new(|| pattern("[\\x00-\\x1f\\x7f`$|;&<>]"));
static CONTROL: LazyLock<Regex> = LazyLock::new(|| pattern("[\\x00-\\x1f\\x7f`]"));
static SHELL_LABEL: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(?:/\S*/)?(?:ba|z)?sh\s+-l?c\s"));
static HEREDOC_START: LazyLock<Regex> = LazyLock::new(|| {
    pattern("^<<(-?)[ \\t]*(?:'([^'\\n]+)'|\"([^\"\\n]+)\"|(\\\\?)([A-Za-z0-9_.-]+))")
});
static PATH_WORD: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9._/~+-]{1,240}$"));
static FLAG: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^(?:-[A-Za-z]{1,3}|--[a-z][a-z0-9-]{1,30})$"));
static ASSIGNMENT: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?s)^[A-Za-z_][A-Za-z0-9_]*=.*$"));
static WRAPPER_OPTION: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^(?:-o[LN0]|-[ei][LN0]|-n?-?\d+)$"));
static DURATION: LazyLock<Regex> = LazyLock::new(|| pattern(r"^\d+(?:\.\d+)?[smhd]?$"));
static HEREDOC_LINE: LazyLock<Regex> =
    LazyLock::new(|| pattern("^\\s*(?:cd\\s+([^\\s;&|'\"]+)\\s*&&\\s*)?([^;&|\\n'\"]*)\\s*$"));
static TO_NOWHERE: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?:^|\s)[12]?>\s*/dev/null\b"));
static REDIRECT: LazyLock<Regex> = LazyLock::new(|| pattern(r"(>>?)"));
static SHORTENED: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(head|tail)(\s+-n)?\s+-?\d+$"));
static LINES_OF: LazyLock<Regex> = LazyLock::new(|| pattern(r"^\d+(,\d+)?p$"));
static WEB: LazyLock<Regex> = LazyLock::new(|| pattern(r"^https?://"));
// Python's `$` also stands before a line end that ends the text.
static SCRIPT_FILE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\.(py|js|mjs|cjs|ts|sh|bash|rb)\n?$"));
static NUMBERED_PYTHON: LazyLock<Regex> = LazyLock::new(|| pattern(r"^python3\.\d+$"));
static FILE_LIKE: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[^\s]+\.[A-Za-z0-9]{1,8}$"));

/// Which library of templates this is: a sentence said under one is kept under its number.
pub fn version() -> Value {
    LIBRARY["version"].clone()
}

/// A template of the library, by its name.
fn template(id: &str) -> Option<&'static Value> {
    LIBRARY["templates"].get(id)
}

/// The last part of a path, as Python's `PurePosixPath(path).name` gives it.
fn last_part(path: &str) -> &str {
    let named = path
        .split('/')
        .rfind(|part| !part.is_empty() && *part != ".");
    named.unwrap_or_default()
}

fn text(value: &str) -> Value {
    Value::String(value.to_string())
}

/// No template is said to fit, and why.
fn abstain(reason: &str, action: &str, signature: &str, candidates: &[String]) -> Value {
    let candidates: Vec<&String> = candidates.iter().take(8).collect();
    json!({
        "reason": reason, "action": action, "signature": signature, "candidates": candidates,
    })
}

/// A template fits, with what the use names for it. What it names as nothing is left out.
fn matched(id: &str, parameters: &[(&str, &str)], signature: &str) -> Value {
    let named: Map<String, Value> = parameters
        .iter()
        .filter(|(_, value)| !value.is_empty())
        .map(|(key, value)| ((*key).to_string(), text(value)))
        .collect();
    let action = template(id).map_or(Value::Null, |template| template["action"].clone());
    json!({
        "template_id": id, "parameters": named, "action": action, "signature": signature,
    })
}

// ---- the shell ---------------------------------------------------------------------------

/// A command split at the shell's operators outside of quotes: each part with the operator
/// after it. `Ok(None)`: it substitutes, redirects or groups, and is not read further.
/// `Err`: a quotation is not closed. That diagnostics are merged or dropped (`2>&1`,
/// `2>/dev/null`) changes nothing of what a command does, and is left out.
fn segments(command: &str) -> Result<Option<Vec<(String, String)>>, ()> {
    let signs: Vec<char> = command.chars().collect();
    let begins = |at: usize, with: &str| {
        let with: Vec<char> = with.chars().collect();
        signs.len() >= at + with.len() && signs[at..at + with.len()] == with[..]
    };
    let mut found: Vec<(String, String)> = Vec::new();
    let (mut current, mut quote, mut i) = (String::new(), None::<char>, 0);
    while i < signs.len() {
        let sign = signs[i];
        if let Some(open) = quote {
            current.push(sign);
            if sign == '\\' && open == '"' && i + 1 < signs.len() {
                current.push(signs[i + 1]);
                i += 1;
            } else if sign == open {
                quote = None;
            }
        } else if sign == '\'' || sign == '"' {
            quote = Some(sign);
            current.push(sign);
        } else if sign == '\\' && i + 1 < signs.len() {
            current.push(sign);
            current.push(signs[i + 1]);
            i += 1;
        } else if sign == '`' || begins(i, "$(") || sign == '(' || sign == ')' {
            return Ok(None);
        } else if (begins(i, "2>&1") || begins(i, "2>/dev/null"))
            && (i == 0 || python::blank(signs[i - 1]))
        {
            i += if begins(i, "2>&1") { 4 } else { 11 };
            continue;
        } else if sign == '<' || sign == '>' {
            return Ok(None);
        } else if matches!(sign, '|' | '&' | ';' | '\n') {
            let operator = if begins(i, "&&") || begins(i, "||") {
                signs[i..i + 2].iter().collect::<String>()
            } else {
                sign.to_string()
            };
            let part = std::mem::take(&mut current);
            i += operator.chars().count();
            found.push((python::strip(&part).to_string(), operator));
            continue;
        } else {
            current.push(sign);
        }
        i += 1;
    }
    if quote.is_some() {
        return Err(());
    }
    found.push((python::strip(&current).to_string(), String::new()));
    Ok(Some(found))
}

/// The script of a command that is a shell started for one script (`bash -lc '...'`); any
/// other command as it is. `None`: it begins as such a command and cannot be split.
pub fn strip_wrapper(command: &str) -> Option<String> {
    if !SHELL_LABEL.is_match(python::strip(command)) {
        return Some(command.to_string());
    }
    let words = tools::shell_split(command)?;
    let shell = ["bash", "sh", "zsh"];
    let wrapped = words.len() == 3
        && shell.contains(&last_part(&words[0]))
        && (words[1] == "-c" || words[1] == "-lc");
    Some(if wrapped {
        words[2].clone()
    } else {
        command.to_string()
    })
}

/// A command with its heredocs taken out and its comments dropped.
pub struct Lifted {
    /// The command with [`HEREDOC`] where a heredoc stood, and no body.
    pub text: String,
    /// The command with every heredoc written the one way, and what it holds withheld.
    pub withheld: String,
    /// What each heredoc held, and whether its end mark was quoted, so that nothing in
    /// it is expanded.
    pub bodies: Vec<(String, bool)>,
}

/// Takes the heredocs out of a command. `None`: a heredoc does not end, or it is a
/// here-string.
pub fn lift_heredocs(command: &str) -> Option<Lifted> {
    let (mut text, mut withheld) = (String::new(), String::new());
    let mut bodies: Vec<(String, bool)> = Vec::new();
    // The heredocs whose bodies begin at the next line end: the end mark, whether leading
    // tabs are taken off, whether it was quoted.
    let mut pending: Vec<(String, bool, bool)> = Vec::new();
    let (mut quote, mut i, n) = (None::<char>, 0, command.len());
    while i < n {
        let rest = &command[i..];
        let sign = rest.chars().next()?;
        let wide = sign.len_utf8();
        let next = rest[wide..].chars().next();
        if let Some(open) = quote {
            text.push(sign);
            withheld.push(sign);
            i += wide;
            if let (true, Some(next)) = (sign == '\\' && open == '"', next) {
                text.push(next);
                withheld.push(next);
                i += next.len_utf8();
            } else if sign == open {
                quote = None;
            }
        } else if sign == '\'' || sign == '"' {
            quote = Some(sign);
            text.push(sign);
            withheld.push(sign);
            i += wide;
        } else if let (true, Some(next)) = (sign == '\\', next) {
            for sign in [sign, next] {
                text.push(sign);
                withheld.push(sign);
            }
            i += wide + next.len_utf8();
        } else if sign == '#'
            && command[..i]
                .chars()
                .next_back()
                .is_none_or(|before| " \t\n;&|(".contains(before))
        {
            i = rest.find('\n').map_or(n, |end| i + end);
        } else if rest.starts_with("<<") {
            if rest.starts_with("<<<") {
                return None;
            }
            let found = HEREDOC_START.captures(rest)?;
            let named = |group: usize| found.get(group).map(|part| part.as_str());
            let mark = named(2).or(named(3)).or(named(5)).unwrap_or_default();
            let tabs = named(1).is_some_and(|dash| !dash.is_empty());
            let quoted = named(5).is_none() || named(4).is_some_and(|slash| !slash.is_empty());
            pending.push((mark.to_string(), tabs, quoted));
            let spaced = withheld.chars().next_back().is_none_or(python::blank);
            text.push(' ');
            text.push_str(HEREDOC);
            text.push(' ');
            if !spaced {
                withheld.push(' ');
            }
            withheld.push_str(WITHHELD_HEREDOC);
            i += found.get(0).map_or(2, |all| all.end());
        } else if sign == '\n' && !pending.is_empty() {
            withheld.push_str(&WITHHELD_BODY.repeat(pending.len()));
            text.push('\n');
            i += 1;
            for (mark, tabs, quoted) in std::mem::take(&mut pending) {
                let mut lines: Vec<&str> = Vec::new();
                loop {
                    if i >= n {
                        return None;
                    }
                    let end = command[i..].find('\n').map(|end| i + end);
                    let line = &command[i..end.unwrap_or(n)];
                    i = end.map_or(n, |end| end + 1);
                    let read = if tabs {
                        line.trim_start_matches('\t')
                    } else {
                        line
                    };
                    if read == mark {
                        break;
                    }
                    lines.push(line);
                }
                bodies.push((lines.join("\n"), quoted));
            }
            if i < n {
                withheld.push('\n');
            }
        } else {
            text.push(sign);
            withheld.push(sign);
            i += wide;
        }
    }
    if quote.is_some() || !pending.is_empty() {
        return None;
    }
    Some(Lifted {
        text,
        withheld,
        bodies,
    })
}

/// Whether what a heredoc holds runs commands itself: a substitution where nothing was
/// quoted.
pub fn expanding(body: &str, quoted: bool) -> bool {
    !quoted && (body.contains("$(") || body.contains('`'))
}

/// The flags a wrapper takes a value after.
fn wrapper_value_flags(wrapper: &str) -> &'static [&'static str] {
    match wrapper {
        "timeout" => &["-s", "-k", "--signal", "--kill-after"],
        "nice" => &["-n", "--adjustment"],
        "ionice" => &["-c", "-n", "-p", "--class", "--classdata"],
        "env" => &["-u", "-C", "--unset", "--chdir"],
        "sudo" => &[
            "-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user", "--group",
        ],
        "time" => &["-f", "-o", "--format", "--output"],
        _ => &[],
    }
}

/// How many words at the beginning of a command only change how it runs: assignments
/// before it, and wrappers such as `timeout 60`, `env A=1`, `nice -n 5`; with `sudo` among
/// them when it is asked for. None where a wrapper's options cannot be read or nothing
/// runs.
pub fn peel(words: &[String], sudo: bool) -> usize {
    let mut i = 0;
    while i < words.len() {
        if ASSIGNMENT.is_match(&words[i]) {
            i += 1;
            continue;
        }
        let base = last_part(&words[i]);
        if !WRAPPERS.contains(&base) || (base == "sudo" && !sudo) {
            break;
        }
        i += 1;
        while i < words.len() && words[i].starts_with('-') && words[i] != "-" {
            if words[i] == "--" {
                i += 1;
                break;
            }
            let name = words[i].split('=').next().unwrap_or_default();
            if !FLAG.is_match(name) && !WRAPPER_OPTION.is_match(&words[i]) {
                return 0;
            }
            let valued = wrapper_value_flags(base).contains(&words[i].as_str());
            i += if valued { 2 } else { 1 };
        }
        if base == "timeout" {
            if i >= words.len() || !DURATION.is_match(&words[i]) {
                return 0;
            }
            i += 1;
        }
    }
    if i >= words.len() { 0 } else { i }
}

/// What a simple command does with the one heredoc it reads: `script` (an interpreter
/// runs it), `write` or `append` (it is stored in a file), or nothing that is known.
pub fn heredoc_consumer(words: &[String]) -> &'static str {
    if words.iter().filter(|word| *word == HEREDOC).count() != 1 {
        return "";
    }
    let plain: Vec<String> = words
        .iter()
        .filter(|word| *word != HEREDOC)
        .cloned()
        .collect();
    let rest = &plain[peel(&plain, true)..];
    let Some(first) = rest.first() else {
        return "";
    };
    let base = last_part(first);
    if base == "cat" {
        let stored = rest.len() == 3 && PATH_WORD.is_match(&rest[2]);
        return match (stored, rest.get(1).map(String::as_str)) {
            (true, Some(">")) => "write",
            (true, Some(">>")) => "append",
            _ => "",
        };
    }
    if base == "tee" {
        let append = rest.get(1).is_some_and(|word| word == "-a");
        let from = if append { 2 } else { 1 };
        let paths = rest.get(from..).unwrap_or_default();
        let stored = paths.len() == 1 && PATH_WORD.is_match(&paths[0]);
        return match (stored, append) {
            (true, true) => "append",
            (true, false) => "write",
            _ => "",
        };
    }
    if SCRIPT.contains(&base) || NUMBERED_PYTHON.is_match(base) {
        let refused = [
            "-c",
            "-e",
            "-m",
            "-E",
            "-p",
            "-r",
            "--eval",
            "--print",
            "--require",
            "--import",
        ];
        let mut args = &rest[1..];
        while let Some(arg) = args.first().map(String::as_str) {
            if !arg.starts_with('-') || arg == "-" {
                break;
            }
            if refused.contains(&arg) || !FLAG.is_match(arg) {
                return "";
            }
            if arg == "-s" {
                return "script";
            }
            args = &args[1..];
        }
        return match args.first().map(String::as_str) {
            None | Some("-") => "script",
            Some(_) => "",
        };
    }
    ""
}

/// A command that reads a heredoc, told by what reads it.
fn classify_heredoc(lifted: &str) -> Value {
    let compound = || abstain("compound", "compound", "shell", &[]);
    let Some(found) = HEREDOC_LINE.captures(lifted) else {
        return compound();
    };
    if lifted.matches(HEREDOC).count() != 1 {
        return compound();
    }
    let command = found.get(2).map_or("", |part| part.as_str());
    let command = TO_NOWHERE.replace_all(command, " ");
    let command = REDIRECT.replace_all(&command, " $1 ");
    let words: Vec<String> = command
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .map(str::to_string)
        .collect();
    match heredoc_consumer(&words) {
        "write" => {
            let plain: Vec<String> = words
                .iter()
                .filter(|word| *word != HEREDOC)
                .cloned()
                .collect();
            let rest = &plain[peel(&plain, true)..];
            let file = match rest.iter().position(|word| word == ">") {
                Some(at) => rest.get(at + 1),
                None => rest.last(),
            };
            let file = file.map_or("", String::as_str);
            matched("write_file", &[("file", file)], &signature(rest))
        }
        "append" => abstain("uncertain", "write", "shell", &[]),
        "script" => abstain("inline_script", "execute", "shell", &[]),
        _ => compound(),
    }
}

/// The flags of a command and what is left when they and their values are taken out.
fn flags_and_args<'a>(
    words: impl IntoIterator<Item = &'a String>,
    value_flags: &[&str],
) -> (Vec<&'a str>, Vec<&'a str>) {
    let (mut flags, mut args, mut skip) = (Vec::new(), Vec::new(), false);
    for word in words {
        if skip {
            skip = false;
        } else if word.starts_with('-') && word != "-" {
            flags.push(word.split('=').next().unwrap_or_default());
            skip = value_flags.contains(&word.as_str());
        } else {
            args.push(word.as_str());
        }
    }
    (flags, args)
}

/// The word after the first of `names` among the words of a command.
fn after<'a>(words: &'a [String], names: &[&str]) -> Option<&'a str> {
    let most = words.len().saturating_sub(1);
    let at = words[..most]
        .iter()
        .position(|word| names.contains(&word.as_str()))?;
    Some(words[at + 1].as_str())
}

/// A value as Python's `json.dumps` writes it by default: a space after every comma, and
/// only ASCII.
fn written(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(written).collect();
            format!("[{}]", items.join(", "))
        }
        plain => python::compact_ascii(plain),
    }
}

/// What stands for the exact shape of a use, under a key that is the Host's own: two uses
/// share it only when every word of the shape is the same. Nothing of the use is kept,
/// and without the key nobody can try a guess against it.
fn identity(key: &str, kind: &str, shape: &Value) -> String {
    let said = written(&json!([kind, shape]));
    let digest = portraits::hmac_sha256_hex(key.as_bytes(), said.as_bytes());
    python::first(&digest, 24).to_string()
}

/// The program of a command and its flags, each flag once. A flag that could carry a
/// value in itself is not named.
fn signature(words: &[String]) -> String {
    let Some(first) = words.first() else {
        return String::new();
    };
    let flags: BTreeSet<&str> = words[1..]
        .iter()
        .filter(|word| word.starts_with('-') && !python::isdigit(word.trim_start_matches('-')))
        .map(|word| {
            let name = word.split('=').next().unwrap_or_default();
            if FLAG.is_match(name) { name } else { "-?" }
        })
        .collect();
    let program = last_part(first);
    let mut said = vec![if NAME.is_match(program) { program } else { "?" }];
    said.extend(flags);
    python::first(&said.join(" "), 120).to_string()
}

/// The host a web address names, as Python's `urlsplit(address).hostname` gives it.
fn host_of(address: &str) -> String {
    let after = address.split_once("://").map_or("", |(_, after)| after);
    let end = after.find(['/', '?', '#']).unwrap_or(after.len());
    let place = &after[..end];
    let place = place.rsplit_once('@').map_or(place, |(_, place)| place);
    let host = match place.split_once('[') {
        Some((_, bracketed)) => bracketed.split(']').next(),
        None => place.split(':').next(),
    };
    host.unwrap_or_default().to_lowercase()
}

/// A shell command, told as one of the kinds there are templates for, or not.
pub fn classify_shell(command: &str, key: &str) -> Value {
    let malformed = || abstain("malformed", "unknown", "shell", &[]);
    let compound = || abstain("compound", "compound", "shell", &[]);
    let Some(mut command) = strip_wrapper(command) else {
        return malformed();
    };
    if command.contains("<<") || command.contains('#') {
        let Some(lifted) = lift_heredocs(&command) else {
            return compound();
        };
        if lifted
            .bodies
            .iter()
            .any(|(body, quoted)| expanding(body, *quoted))
        {
            return compound();
        }
        if !lifted.bodies.is_empty() {
            return classify_heredoc(&lifted.text);
        }
        command = lifted.text;
    }
    let mut parts = match segments(&command) {
        Err(()) => return malformed(),
        Ok(None) => return compound(),
        Ok(Some(parts)) => parts,
    };
    let mut cwd = String::new();
    if parts.len() >= 2 && parts[0].1 == "&&" {
        let Some(first) = tools::shell_split(&parts[0].0) else {
            return malformed();
        };
        if first.len() == 2 && first[0] == "cd" {
            cwd.clone_from(&first[1]);
            parts.remove(0);
        }
    }
    // A `| head -n N` or `| tail -N` at the end only shortens what is shown.
    if parts.len() == 2 && parts[0].1 == "|" && SHORTENED.is_match(&parts[1].0) {
        parts.truncate(1);
    }
    if parts.len() != 1 || parts[0].0.is_empty() {
        return compound();
    }
    let Some(words) = tools::shell_split(&parts[0].0) else {
        return malformed();
    };
    // Assignments and wrappers only change how the command runs. `sudo` stays: it says
    // with whose rights.
    let words = &words[peel(&words, false)..];
    if words.is_empty() {
        return malformed();
    }
    classify_words(words, &cwd, key)
}

fn classify_words(words: &[String], cwd: &str, key: &str) -> Value {
    let base = last_part(&words[0]);
    let said = signature(words);
    let said = said.as_str();
    let (flags, args) = flags_and_args(&words[1..], &VALUE_FLAGS);
    if PRIVILEGED.contains(&base) {
        return abstain("privileged", "privileged", said, &[]);
    }
    if ["ls", "tree", "dir"].contains(&base) {
        if args.len() > 1 {
            return abstain("multiple_targets", "list", said, &[]);
        }
        let directory = args.first().copied().unwrap_or(cwd);
        return matched("list_directory", &[("directory", directory)], said);
    }
    if base == "pwd" {
        return matched("show_current_directory", &[], said);
    }
    if READ.contains(&base) {
        let files: Vec<&str> = args
            .iter()
            .copied()
            .filter(|arg| !python::isdigit(arg))
            .collect();
        return match files[..] {
            [file] => matched("read_file", &[("file", file)], said),
            _ => abstain("multiple_targets", "read", said, &[]),
        };
    }
    if base == "sed" {
        if flags
            .iter()
            .any(|flag| flag.starts_with("-i") || *flag == "--in-place")
        {
            return match args[..] {
                [_, file] => matched("edit_file", &[("file", file)], said),
                _ => abstain("uncertain", "edit", said, &[]),
            };
        }
        if flags.contains(&"-n") && args.len() == 2 && LINES_OF.is_match(args[0]) {
            return matched("read_file", &[("file", args[1])], said);
        }
        return abstain("uncertain", "unknown", said, &[]);
    }
    if SEARCH.contains(&base) {
        if flags.contains(&"-f") || flags.contains(&"--file") {
            return abstain("uncertain", "search", said, &[]);
        }
        if flags.contains(&"--files") {
            return match args[..] {
                [] => matched("find_files", &[("directory", cwd)], said),
                [directory] => matched("find_files", &[("directory", directory)], said),
                _ => abstain("multiple_targets", "list", said, &[]),
            };
        }
        let named = after(words, &["-e", "--regexp"]).unwrap_or_default();
        let (pattern, paths) = if !named.is_empty() {
            // Named by a flag: every other word that is no flag is where to search.
            let others = words[1..].iter().filter(|word| *word != named);
            (named, flags_and_args(others, &VALUE_FLAGS).1)
        } else if let Some((pattern, paths)) = args.split_first() {
            (*pattern, paths.to_vec())
        } else {
            return abstain("malformed", "search", said, &[]);
        };
        let directory = match paths[..] {
            [] => cwd,
            [directory] => directory,
            _ => "",
        };
        let named = [("pattern", pattern), ("directory", directory)];
        return matched("search_text", &named, said);
    }
    if base == "find" {
        let acts = [
            "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls",
        ];
        if words.iter().any(|word| acts.contains(&word.as_str())) {
            return abstain("find_action", "compound", said, &[]);
        }
        let directory = match words.get(1) {
            Some(first) if !first.starts_with('-') => first.as_str(),
            _ => cwd,
        };
        let pattern = after(words, &["-name", "-iname", "-path", "-ipath"]).unwrap_or_default();
        let named = [("pattern", pattern), ("directory", directory)];
        return matched("find_files", &named, said);
    }
    if base == "fd" || base == "fdfind" {
        let acts = ["-x", "--exec", "-X", "--exec-batch"];
        if flags.iter().any(|flag| acts.contains(flag)) {
            return abstain("find_action", "compound", said, &[]);
        }
        if args.len() > 2 {
            return abstain("multiple_targets", "list", said, &[]);
        }
        let pattern = args.first().copied().unwrap_or_default();
        let directory = args.get(1).copied().unwrap_or(cwd);
        let named = [("pattern", pattern), ("directory", directory)];
        return matched("find_files", &named, said);
    }
    if ["rm", "rmdir", "unlink"].contains(&base) {
        if args.is_empty() {
            return abstain("malformed", "delete", said, &[]);
        }
        let recursive = base == "rm"
            && flags.iter().any(|flag| {
                *flag == "--recursive"
                    || (!flag.starts_with("--") && flag[1..].contains(['r', 'R']))
            });
        let id = if recursive {
            "delete_recursive"
        } else {
            "delete_path"
        };
        let path = if args.len() == 1 { args[0] } else { "" };
        return matched(id, &[("path", path)], said);
    }
    if base == "mkdir" {
        return match args[..] {
            [directory] => matched("create_directory", &[("directory", directory)], said),
            _ => abstain("multiple_targets", "write", said, &[]),
        };
    }
    if base == "curl" || base == "wget" {
        let uploads = [
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-urlencode",
            "-F",
            "--form",
            "-T",
            "--upload-file",
            "--json",
            "--post-data",
            "--post-file",
            "--body-data",
            "--body-file",
        ];
        let method = after(words, &["-X", "--request", "--method"]).unwrap_or("GET");
        let asks = ["GET", "HEAD"].contains(&method.to_uppercase().as_str());
        if flags.iter().any(|flag| uploads.contains(flag)) || !asks {
            return abstain("network_upload", "network", said, &[]);
        }
        let urls: Vec<&String> = words[1..]
            .iter()
            .filter(|word| WEB.is_match(word))
            .collect();
        let [url] = urls[..] else {
            return abstain("uncertain", "network", said, &[]);
        };
        let host = host_of(url);
        return matched("fetch_url", &[("host", host.as_str())], said);
    }
    if base == "git" {
        return classify_git(words);
    }
    let tests = base == "pytest" || base == "py.test";
    let through_python = words.len() >= 3
        && (words[0] == "python" || words[0] == "python3")
        && words[1] == "-m"
        && words[2] == "pytest";
    if tests || through_python {
        let valued = ["-k", "-m", "-n", "-p", "-c", "--rootdir", "--maxfail", "-o"];
        let from = if tests { 1 } else { 3 };
        let (_, targets) = flags_and_args(&words[from..], &valued);
        let target = if targets.len() == 1 { targets[0] } else { "" };
        return matched("run_tests", &[("target", target)], said);
    }
    if SCRIPT.contains(&base) || SCRIPT_FILE.is_match(base) {
        return abstain("script_run", "execute", said, &[]);
    }
    if MUTATING.contains(&base) {
        return abstain("mutating_program", "execute", said, &[]);
    }
    if BUILTINS.contains(&base) {
        return abstain("shell_builtin", "unknown", said, &[]);
    }
    let candidates: Vec<String> = args
        .iter()
        .filter(|arg| (1..=240).contains(&arg.chars().count()))
        .map(|arg| (*arg).to_string())
        .collect();
    // Only one thing a program is given may differ between two uses that count as the
    // same: everything else, the values of its options too, is part of what it is asked.
    let lone = match &candidates[..] {
        [lone] => Some(lone.as_str()),
        _ => None,
    };
    let mut shape = vec![text(&words[0])];
    shape.extend(words[1..].iter().map(|word| match lone {
        Some(lone) if lone == word => text("{arg}"),
        _ => text(word),
    }));
    let display = if NAME.is_match(base) { base } else { "?" };
    let signed = format!(
        "{display} #{}",
        identity(key, "shell", &Value::Array(shape))
    );
    abstain("unknown_program", "unknown", &signed, &candidates)
}

fn classify_git(words: &[String]) -> Value {
    let mut rest = &words[1..];
    while let Some(first) = rest.first() {
        match first.as_str() {
            "--no-pager" => rest = &rest[1..],
            "-C" | "-c" => rest = rest.get(2..).unwrap_or_default(),
            _ => break,
        }
    }
    let Some((command, more)) = rest.split_first() else {
        return abstain("malformed", "unknown", &signature(words), &[]);
    };
    let (flags, args) = flags_and_args(more, &VALUE_FLAGS);
    let said = format!("git {command}");
    let simple = match command.as_str() {
        "status" => Some("git_status"),
        "diff" => Some("git_diff"),
        "log" => Some("git_log"),
        "pull" if args.is_empty() => Some("git_pull"),
        "commit" => Some("git_commit"),
        _ => None,
    };
    if let Some(id) = simple {
        return matched(id, &[], &said);
    }
    if command == "push" {
        let forced = [
            "-f",
            "--force",
            "--force-with-lease",
            "--delete",
            "-d",
            "--mirror",
            "--prune",
        ];
        if flags.iter().any(|flag| forced.contains(flag))
            || args.len() > 2
            || args
                .iter()
                .any(|arg| arg.contains(':') || arg.starts_with('+'))
        {
            return abstain("unmapped_git", "network", &said, &[]);
        }
        let remote = args.first().copied().unwrap_or_default();
        let branch = args.get(1).copied().unwrap_or_default();
        return matched("git_push", &[("remote", remote), ("branch", branch)], &said);
    }
    if command == "grep" && !args.is_empty() {
        let directory = if args.len() == 2 { args[1] } else { "" };
        let named = [("pattern", args[0]), ("directory", directory)];
        return matched("search_text", &named, &said);
    }
    if command == "ls-files" {
        let directory = if args.len() == 1 { args[0] } else { "" };
        return matched("find_files", &[("directory", directory)], &said);
    }
    abstain("unmapped_git", "unknown", &said, &[])
}

// ---- named tools ---------------------------------------------------------------------------

/// What a use says under one of several names, in itself or in what it was given: the
/// first that is a text and says something, without the blanks around it.
fn field<'a>(activity: &'a Map<String, Value>, keys: &[&str]) -> &'a str {
    let inputs = activity.get("input").and_then(Value::as_object);
    for key in keys {
        for source in [Some(activity), inputs].into_iter().flatten() {
            let said = source.get(*key).and_then(Value::as_str).map(python::strip);
            if let Some(said) = said.filter(|said| !said.is_empty()) {
                return said;
            }
        }
    }
    ""
}

/// What a value says where Python takes it as a text (`str(value or "")`).
fn said_of(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(said)) => said.clone(),
        Some(other) if python::truthy(other) => python::repr(other),
        _ => String::new(),
    }
}

/// The shell command the name of a use spells out, where the use is given as nothing
/// else: so the rows of Codex come when an app has only what it showed
/// (`/usr/bin/bash -lc "..."`).
pub fn shell_label(activity: &Map<String, Value>) -> String {
    let name = said_of(activity.get("name"));
    let name = python::strip(&name);
    let of_a_kind = activity.get("kind").is_some_and(python::truthy);
    let commanded = !field(activity, &["command", "cmd"]).is_empty();
    if of_a_kind || commanded || !SHELL_LABEL.is_match(name) {
        return String::new();
    }
    name.to_string()
}

/// The texts among a list of values.
fn texts(value: Option<&Value>) -> Vec<&str> {
    let items = value.and_then(Value::as_array);
    items
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect()
}

/// A use of a tool, as it was read, told as one of the kinds there are templates for, or
/// not. `key`: the Host's own, under which the shape of a use nobody listed is signed.
pub fn classify(activity: &Map<String, Value>, key: &str) -> Value {
    let name = said_of(activity.get("name"));
    let name = python::strip(&name);
    let kind = said_of(activity.get("kind"));
    let kind = python::strip(&kind);
    let tool = name.to_lowercase();
    let said = format!("tool:{}", if name.is_empty() { kind } else { name });
    let said = python::first(&said, 120);
    let malformed = || abstain("malformed", "unknown", said, &[]);
    let summary = activity
        .get("summary")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let command = match field(activity, &["command", "cmd"]) {
        "" if kind == "command" => summary,
        command => command,
    };
    let shells = ["bash", "shell", "exec_command", "local_shell"];
    if shells.contains(&tool.as_str()) || kind == "command" {
        return if command.is_empty() {
            malformed()
        } else {
            classify_shell(command, key)
        };
    }
    let label = shell_label(activity);
    if !label.is_empty() {
        return if label.chars().count() >= LABEL_CLIP {
            abstain("truncated", "compound", "shell", &[])
        } else {
            classify_shell(&label, key)
        };
    }
    let file_path = field(activity, &["file_path", "path"]);
    let of_a_file = |id: &str| {
        if file_path.is_empty() {
            malformed()
        } else {
            matched(id, &[("file", file_path)], said)
        }
    };
    match tool.as_str() {
        "read" => return of_a_file("read_file"),
        "edit" | "multiedit" | "notebookedit" => return of_a_file("edit_file"),
        "write" => return of_a_file("write_file"),
        "grep" => {
            let pattern = field(activity, &["pattern"]);
            if pattern.is_empty() {
                return malformed();
            }
            let named = [
                ("pattern", pattern),
                ("directory", field(activity, &["path"])),
            ];
            return matched("search_text", &named, said);
        }
        "glob" => {
            let named = [
                ("pattern", field(activity, &["pattern"])),
                ("directory", field(activity, &["path"])),
            ];
            return matched("find_files", &named, said);
        }
        "ls" => {
            let named = [("directory", field(activity, &["path"]))];
            return matched("list_directory", &named, said);
        }
        "websearch" => {
            let query = field(activity, &["query"]);
            return if query.is_empty() {
                malformed()
            } else {
                matched("web_search", &[("query", query)], said)
            };
        }
        _ => {}
    }
    if kind == "exploration" {
        return classify_exploration(&texts(activity.get("operations")));
    }
    if kind == "patch" {
        let operations = texts(activity.get("operations"));
        if let [operation] = operations[..]
            && let Some((label, path)) = operation.split_once(": ")
        {
            let fits = match label {
                "Edit" => Some(("edit_file", "file")),
                "Add" => Some(("write_file", "file")),
                "Delete" => Some(("delete_path", "path")),
                _ => None,
            };
            if let Some((id, named)) = fits {
                return matched(id, &[(named, path)], &format!("patch:{label}"));
            }
        }
        return abstain("multiple_targets", "edit", "patch", &[]);
    }
    if name.is_empty() && kind.is_empty() {
        return malformed();
    }
    // What the use says in texts, in itself and in what it was given, but for the file it
    // is about: the shape of a tool nobody listed.
    let mut shown: BTreeMap<&str, &Value> = BTreeMap::new();
    let own = activity.iter().filter(|(_, value)| value.is_string());
    shown.extend(own.map(|(key, value)| (key.as_str(), value)));
    let given = activity.get("input").and_then(Value::as_object);
    shown.extend(
        given
            .into_iter()
            .flatten()
            .map(|(key, value)| (key.as_str(), value)),
    );
    let pairs: Vec<Value> = shown
        .iter()
        .filter_map(|(key, value)| Some((key, value.as_str()?)))
        .filter(|(_, value)| *value != file_path)
        .map(|(key, value)| json!([key, value]))
        .collect();
    let signed = format!(
        "{said} #{}",
        identity(key, "tool", &json!([name, kind, pairs]))
    );
    let candidates: Vec<String> = if file_path.is_empty() {
        Vec::new()
    } else {
        vec![file_path.to_string()]
    };
    abstain("unknown_tool", "unknown", &signed, &candidates)
}

/// A row that stands for several looks. Only a single `Read: path` names what it reads.
fn classify_exploration(operations: &[&str]) -> Value {
    let mut once: Vec<&str> = Vec::new();
    for operation in operations {
        if !once.contains(operation) {
            once.push(operation);
        }
    }
    if let [operation] = once[..] {
        let (label, target) = operation.split_once(": ").unwrap_or((operation, ""));
        let begins = target.starts_with(['.', '~']);
        let a_file = target.contains('/') || begins || FILE_LIKE.is_match(target);
        if label == "Read"
            && !target.is_empty()
            && !target.contains(',')
            && !target.chars().any(python::blank)
            && a_file
        {
            return matched("read_file", &[("file", target)], "exploration:Read");
        }
    }
    let reason = if once.len() > 1 {
        "multiple_targets"
    } else {
        "exploration"
    };
    abstain(reason, "read", "exploration", &[])
}

// ---- parameters and sentences --------------------------------------------------------------

/// Everything a use shows in texts: where a parameter has to stand literally.
fn evidence(activity: &Map<String, Value>) -> String {
    let mut shown: Vec<&str> = activity.values().filter_map(Value::as_str).collect();
    match activity.get("input") {
        Some(Value::Object(inputs)) => shown.extend(inputs.values().filter_map(Value::as_str)),
        Some(Value::String(input)) => shown.push(input),
        _ => {}
    }
    shown.extend(texts(activity.get("operations")));
    shown.join("\n")
}

/// Whether a value is what a parameter of its kind may be.
pub fn valid_parameter(kind: &str, value: &str) -> bool {
    let kinds = ["directory", "path", "pattern", "host", "name", "text"];
    if !kinds.contains(&kind) || value.is_empty() || value != python::strip(value) {
        return false;
    }
    if value.contains("[redacted]") {
        return false;
    }
    let long = value.chars().count();
    match kind {
        "directory" | "path" => {
            long <= 240
                && !value.starts_with('-')
                && !UNSAFE.is_match(value)
                && !value.contains("://")
        }
        "pattern" => long <= 120 && !CONTROL.is_match(value),
        "host" => HOST.is_match(value) && value.contains('.'),
        "name" => NAME.is_match(value),
        _ => long <= 160 && !CONTROL.is_match(value),
    }
}

/// The parameters of a template as they may be said: each of its kind, and each standing
/// literally in what the use shows. `None`: one of them is not.
pub fn validate(
    id: &str,
    parameters: &Value,
    activity: &Map<String, Value>,
) -> Option<Map<String, Value>> {
    let kinds = template(id)?["params"].as_object()?;
    let parameters = parameters.as_object()?;
    let shown = evidence(activity);
    for (key, value) in parameters {
        let kind = kinds.get(key)?.as_str()?;
        let value = value.as_str()?;
        if !valid_parameter(kind, value) || !shown.contains(value) {
            return None;
        }
    }
    Some(parameters.clone())
}

/// What can be said beside the parameters themselves: the last part of a path, and the
/// project a directory is.
fn derived(parameters: &Map<String, Value>) -> Map<String, Value> {
    let mut values = parameters.clone();
    for key in ["directory", "file", "path", "target"] {
        let Some(path) = parameters.get(key).and_then(Value::as_str) else {
            continue;
        };
        let name = last_part(path.trim_end_matches('/'));
        if name.is_empty() || name == ".." || name.starts_with('~') {
            continue;
        }
        let called = if key == "directory" {
            "directory_name"
        } else {
            "file_name"
        };
        if !values.contains_key(called) {
            values.insert(called.to_string(), text(name));
        }
        if key == "directory" && PROJECT.is_match(name) {
            values.insert("project".to_string(), text(name));
        }
    }
    values
}

/// A sentence with its places filled, where everything it names is there.
fn filled(variant: &str, values: &Map<String, Value>) -> Option<String> {
    let mut said = String::new();
    let mut rest = variant;
    while let Some(at) = rest.find('{') {
        said.push_str(&rest[..at]);
        let end = rest[at..].find('}')?;
        said.push_str(values.get(&rest[at + 1..at + end])?.as_str()?);
        rest = &rest[at + end + 1..];
    }
    said.push_str(rest);
    Some(said)
}

/// The sentence of a template at a level of detail: the first of its sentences for that
/// level of which everything it names is there. `None`: the level has none (the first
/// level is the use itself, untranslated), or none can be filled.
pub fn render(id: &str, parameters: &Map<String, Value>, level: u8) -> Option<String> {
    let variants = template(id)?["text"].get(level.to_string())?.as_array()?;
    let values = derived(parameters);
    variants
        .iter()
        .filter_map(Value::as_str)
        .filter_map(|variant| filled(variant, &values))
        .find(|said| said.chars().count() <= 240)
}

/// What is said of a use at a level of detail without asking anybody, and how the use was
/// told: the template that fits with its parameters, or why none does.
pub fn lookup(activity: &Map<String, Value>, level: u8, key: &str) -> (Option<String>, Value) {
    let route = classify(activity, key);
    let Some(id) = route["template_id"].as_str() else {
        return (None, route);
    };
    let Some(parameters) = validate(id, &route["parameters"], activity) else {
        let refused = json!({
            "reason": "invalid_parameters", "action": route["action"],
            "signature": route["signature"],
        });
        return (None, refused);
    };
    (render(id, &parameters, level), route)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_last_part_of_a_path_is_what_python_says_it_is() {
        for (path, name) in [
            ("a/b", "b"),
            ("a/b/", "b"),
            ("/", ""),
            ("", ""),
            (".", ""),
            ("..", ".."),
            ("./a", "a"),
            ("a/.", "a"),
            ("a//b", "b"),
            ("/usr/bin/bash", "bash"),
        ] {
            assert_eq!(last_part(path), name, "{path}");
        }
    }

    #[test]
    fn a_host_is_read_out_of_a_web_address() {
        for (address, host) in [
            ("https://example.org/page", "example.org"),
            ("http://User@Example.ORG:8080/x?y#z", "example.org"),
            ("https://[::1]:8443/", "::1"),
            ("https://", ""),
        ] {
            assert_eq!(host_of(address), host, "{address}");
        }
    }

    #[test]
    fn every_template_of_the_library_says_what_kind_of_action_it_is() {
        let templates = LIBRARY["templates"].as_object().map_or(0, Map::len);
        assert_eq!(templates, 19);
        assert_eq!(matched("git_status", &[], "git status")["action"], "read");
    }
}
