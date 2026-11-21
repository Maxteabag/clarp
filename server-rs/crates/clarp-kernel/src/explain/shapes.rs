//! The parts of a tool's use, and the shape of each.
//!
//! What is said of one use is kept for the next use of the same shape. A shell command
//! is cut into its parts at the shell's operators (`&&`, `||`, `;`, `&`, `|`, a new line);
//! each part becomes a shape: its words, with every value that may differ between two
//! calls replaced by a slot of its kind (a number, a commit's name, an address, a path, a
//! variable, an identifier, free text, an assignment before the command). What stays as it
//! is, is what the call is: the program, plain lower-case words (they usually select what a
//! program does: `list` or `delete`), upper-case words, flags. A part's signature is its
//! program and the Host's own keyed hash of the shape, so that no word of a call is kept.
//!
//! Some forms are told through what they run: a heredoc by what reads it (its body is
//! withheld and is never a slot), `bash -c '...'` as the parts of its script, a
//! substitution that only reads (`$(date +%F)`) as one slot. A part that cannot be shaped
//! safely is exact: its signature is the hash of the whole part, and what was said of it
//! is said again only of the identical text. A `cd DIR` is no part: it is where the parts
//! after it run. A `| head -n N` at the end and `true` are left out.
//!
//! Nothing here runs a command. The rules are the Python Host's
//! (`server/lib/tool_explanation_shapes.py`), held by the `parts` and the `helpers` of
//! `tests/fixtures/tool-explanation-vectors.json`.

use crate::python::shell_quote;
use std::collections::BTreeMap;
use std::sync::LazyLock;

use regex::{NoExpand, Regex};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::{
    BUILTINS, FILE_LIKE, FLAG, HEREDOC, LABEL_CLIP, NAME, NUMBERED_PYTHON, SCRIPT, SHORTENED,
    WITHHELD_BODY, WITHHELD_HEREDOC, expanding, field, heredoc_consumer, host_of, identity,
    last_part, lift_heredocs, pattern, peel, said_of, shell_label, strip_wrapper, text,
};
use crate::{python, tools};

/// A call of more parts than this is told as one.
pub const MAX_PARTS: usize = 8;
/// A shape longer than this, or with more than sixteen slots, is not kept as a shape.
const MAX_SHAPE: usize = 400;

/// What only filters or arranges what it is given: a command may be piped into these.
const FILTERS: &[&str] = &[
    "grep", "rg", "egrep", "fgrep", "head", "tail", "sort", "uniq", "wc", "cut", "tr", "jq",
    "column", "nl", "cat", "sed", "less", "more", "fmt", "fold", "rev", "tac",
];
const KEYWORDS: &[&str] = &[
    "{", "}", "(", ")", "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "select", "[[", "]]",
];
const NOOP: &[&str] = &["true", ":"];
/// The flags after which an interpreter is given its script as a word.
const INLINE: &[&str] = &["-c", "-e", "--eval", "-E", "-p", "--print", "-r"];
const SKIP_SEGMENT: &[&str] = &["for", "case", "select", "function"];
const LEADING: &[&str] = &[
    "do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "time", "exec", "nohup",
];
/// What a substitution may run and stay a plain value of the command around it: it only
/// reads, and prints something short.
const SUBSTITUTE: &[&str] = &[
    "date", "pwd", "whoami", "hostname", "id", "uname", "nproc", "basename", "dirname", "realpath",
    "readlink", "cat", "head", "tail", "ls", "wc", "echo", "printf", "seq", "which", "pgrep",
    "stat", "uuidgen", "jq", "grep", "rg", "sort", "cut", "tr", "ps", "tty",
];
const GIT_READ: &[&str] = &[
    "rev-parse",
    "describe",
    "merge-base",
    "rev-list",
    "symbolic-ref",
    "log",
    "show",
    "ls-files",
];
/// The fields of a named tool that are part of its shape.
const TOOL_FIELDS: &[&str] = &[
    "file_path",
    "path",
    "pattern",
    "query",
    "title",
    "summary",
    "description",
];
/// An unquoted `<` and `>`, as they are written before the words of a part are split, so
/// that a quoted `<b>` is never read as a redirection.
const IN: char = '\u{1}';
const OUT: char = '\u{2}';
/// A substitution that only reads is kept as one word while a part is split: its blanks,
/// quotes and backslashes are written as these until the word is read back.
const PROTECTED: [(char, char); 5] = [
    (' ', '\u{5}'),
    ('\t', '\u{6}'),
    ('\'', '\u{7}'),
    ('"', '\u{8}'),
    ('\\', '\u{e}'),
];

static PROGRAM: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,60}$"));
static SAFE_PATH: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9._/~+-]{1,160}$"));
static WORD: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[a-z]+(?:[-_][a-z]+)*$"));
static VERSIONED: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[a-z]+\d{1,2}$"));
static UPPER: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Z][A-Z_]*$"));
static NUMBER: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[+-]?\d+(?:\.\d+)?[smhdkKMG%]?$"));
static URL: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$"));
static ID: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9][A-Za-z0-9._:@+=,-]{0,160}$"));
static ENV: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?s)^([A-Za-z_][A-Za-z0-9_]*)=(.*)$"));
static MODULE: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$"));
static SHORT_WITH_NUMBER: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(-[A-Za-z])(\d+)$"));
static LONG_WITH_VALUE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"(?s)^(--[a-z][a-z0-9-]{1,30})=(.*)$"));
static DEVNULL: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^(?:[12&]?\x02\x02?|\x02&)/dev/null$|^2\x02&1$|^\x02&2$|^1\x02&2$"));
static REDIRECT: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?s)^([12&]?\x02\x02?|\x01)(.*)$"));
static PLACEHOLDER: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\{([a-z]+[0-9]+(?:_name|_host)?)\}"));
static NUMBER_WORDS: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"(?i)\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|",
        r"fifteen|twenty|thirty|fifty|hundred|thousand|dozen|single|double|twice|once)\b",
    ))
});
static DIGIT: LazyLock<Regex> = LazyLock::new(|| pattern(r"\d"));
static WRAPPER_START: LazyLock<Regex> =
    LazyLock::new(|| pattern("^\\s*(?:/\\S*/)?(?:ba|z)?sh\\s+-l?c\\s+['\"]?"));
static OPERATORS: LazyLock<Regex> = LazyLock::new(|| pattern(r"&&|\|\||[;\n|&]"));
static NOT_ONLY_READING: LazyLock<Regex> = LazyLock::new(|| pattern("[`$<>;&\\n\\\\]"));
static HEREDOC_AMID: LazyLock<Regex> = LazyLock::new(|| pattern(r"\s*\x03\s*"));

/// One piece of a tool's use that is explained by itself.
#[derive(Clone, Debug, PartialEq)]
pub struct Part {
    /// What is shown of this part to whoever explains it.
    pub activity: Map<String, Value>,
    /// What an explanation of its shape is kept under.
    pub signature: String,
    /// What an explanation of exactly this text is kept under; the signature itself where
    /// the part is exact.
    pub exact_key: String,
    /// The program it runs, or the tool it is.
    pub program: String,
    /// The values of this call, by the name of their slot.
    pub slots: Map<String, Value>,
    /// Whether it could not be shaped, and is known by its whole text only.
    pub exact: bool,
    /// Why it is exact.
    pub reason: String,
    /// The directory it runs in, where a `cd` before it said so.
    pub cwd: String,
    /// Names an earlier version kept this part under, each with the name it has now, so
    /// that what was learned then is still found, and moved.
    pub legacy: Vec<(String, String)>,
}

impl Part {
    /// The part as it is written down and sent.
    pub fn to_json(&self) -> Value {
        let legacy: Vec<Value> = self
            .legacy
            .iter()
            .map(|(old, new)| json!([old, new]))
            .collect();
        json!({
            "activity": self.activity, "signature": self.signature, "exact_key": self.exact_key,
            "program": self.program, "slots": self.slots, "exact": self.exact,
            "reason": self.reason, "cwd": self.cwd, "legacy": legacy,
        })
    }

    /// A part read back from what [`Part::to_json`] wrote. What is not there is empty.
    pub fn from_json(value: &Value) -> Self {
        let said = |key: &str| value[key].as_str().unwrap_or_default().to_string();
        let pairs = value["legacy"].as_array().into_iter().flatten();
        Self {
            activity: value["activity"].as_object().cloned().unwrap_or_default(),
            signature: said("signature"),
            exact_key: said("exact_key"),
            program: said("program"),
            slots: value["slots"].as_object().cloned().unwrap_or_default(),
            exact: value["exact"].as_bool().unwrap_or(false),
            reason: said("reason"),
            cwd: said("cwd"),
            legacy: pairs
                .map(|pair| {
                    let at = |index: usize| pair[index].as_str().unwrap_or_default().to_string();
                    (at(0), at(1))
                })
                .collect(),
        }
    }

    /// The part as the templates are asked about it: with the directory it runs in.
    pub fn template_activity(&self) -> Map<String, Value> {
        let mut activity = self.activity.clone();
        if !self.cwd.is_empty() && activity.get("name").and_then(Value::as_str) == Some("Bash") {
            let own = activity
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let command = format!("cd {} && {own}", shell_quote(&self.cwd));
            activity.insert("command".to_string(), text(&command));
            activity.insert("input".to_string(), json!({"command": command}));
        }
        activity
    }
}

/// A part known by its whole text. `previous`: the program an earlier version named it by.
fn exact(
    activity: Map<String, Value>,
    whole: &str,
    reason: &str,
    program: &str,
    previous: &str,
    key: &str,
) -> Part {
    let digest = identity(key, "exact", &text(whole));
    let or_unknown = |name: &str| {
        if name.is_empty() {
            "?".to_string()
        } else {
            name.to_string()
        }
    };
    let signature = format!("x:{} #{digest}", or_unknown(program));
    let old = format!("x:{} #{digest}", or_unknown(previous));
    let legacy = if old == signature {
        Vec::new()
    } else {
        vec![(old, signature.clone())]
    };
    Part {
        activity,
        exact_key: signature.clone(),
        signature,
        program: program.to_string(),
        slots: Map::new(),
        exact: true,
        reason: reason.to_string(),
        cwd: String::new(),
        legacy,
    }
}

/// The program a simple command runs, looking through what only changes how it runs, and
/// the name of its first word: each as a name, or `?`.
fn program(words: &[String]) -> (String, String) {
    let words: Vec<String> = words
        .iter()
        .filter(|word| !ENV.is_match(word))
        .cloned()
        .collect();
    let first = words.first().map_or("?", |word| last_part(word));
    let rest = &words[peel(&words, true)..];
    let runs = rest.first().map_or(first, |word| last_part(word));
    let named = |name: &str| {
        if NAME.is_match(name) {
            name.to_string()
        } else {
            "?".to_string()
        }
    };
    (named(runs), named(first))
}

/// The program a whole command mostly runs, where the command is told as one piece: of its
/// first simple command, with a `cd` before it, assignments, the shell's own words and what
/// does nothing passed over. A program's name only, never what it is given.
pub fn first_program(command: &str) -> String {
    let unwrapped = WRAPPER_START.replace(command, "");
    // What a heredoc holds is never read as commands.
    let read = match lift_heredocs(&unwrapped) {
        Some(lifted) => lifted.text,
        None => unwrapped.into_owned(),
    };
    for segment in OPERATORS.split(&read) {
        let words: Vec<&str> = segment
            .split(python::blank)
            .filter(|word| !word.is_empty())
            .collect();
        let mut from = 0;
        let mut assigned_by_running = false;
        while let Some(word) = words.get(from) {
            let assignment = ENV.is_match(word);
            if !LEADING.contains(word) && !assignment {
                break;
            }
            if assignment && (word.contains("$(") || word.contains('`')) {
                // What is being assigned is not what the command runs.
                assigned_by_running = true;
                break;
            }
            from += 1;
        }
        let left = if assigned_by_running {
            String::new()
        } else {
            words[from..].join(" ")
        };
        // `(cd x && make)`, `$(git ...)`: the program inside the punctuation.
        let words: Vec<String> = left
            .replace("$(", " ")
            .split(python::blank)
            .map(|word| {
                let word = word.trim_start_matches(['(', '{', '!']);
                word.trim_end_matches([')', '}']).trim_matches(['\'', '"'])
            })
            .filter(|word| !word.is_empty() && !LEADING.contains(word))
            .map(str::to_string)
            .collect();
        let Some(first) = words.first() else {
            continue;
        };
        if SKIP_SEGMENT.contains(&first.as_str()) {
            continue;
        }
        let name = program(&words).0;
        let own = [BUILTINS.as_slice(), NOOP, KEYWORDS].concat();
        if name.is_empty() || own.contains(&name.as_str()) || !PROGRAM.is_match(&name) {
            continue;
        }
        return name;
    }
    String::new()
}

// ---- the kind of a word --------------------------------------------------------------------

/// Whether a word could be a commit's name: seven to sixty-four hex signs, a digit and a
/// letter among them.
fn commit_like(word: &str) -> bool {
    let letter = |sign: u8| (b'a'..=b'f').contains(&sign);
    (7..=64).contains(&word.len())
        && word
            .bytes()
            .all(|sign| sign.is_ascii_digit() || letter(sign))
        && word.bytes().any(|sign| sign.is_ascii_digit())
        && word.bytes().any(letter)
}

/// The kind of slot a word is a value of, or `None` where it stays as it is.
pub fn kind(word: &str) -> Option<&'static str> {
    if commit_like(word) {
        return Some("sha");
    }
    if WORD.is_match(word) || VERSIONED.is_match(word) || UPPER.is_match(word) {
        return None;
    }
    if NUMBER.is_match(word) {
        return Some("num");
    }
    if URL.is_match(word) {
        return Some("url");
    }
    if word.contains('$') {
        return Some("expr");
    }
    // A word of nothing begins with whatever is asked, as Python has it.
    let begins = word.is_empty() || word.starts_with(['.', '~']);
    let path_like = word.contains('/') || begins || FILE_LIKE.is_match(word);
    if !word.chars().any(python::blank) && path_like {
        return Some("path");
    }
    if ID.is_match(word) {
        return Some("id");
    }
    Some("text")
}

/// A shape as it grows: the words that stay, the slots in their places, and what this
/// call has in each slot.
#[derive(Default)]
struct Shaper {
    shape: Vec<String>,
    slots: Map<String, Value>,
    counts: BTreeMap<&'static str, usize>,
}

impl Shaper {
    fn literal(&mut self, word: &str) {
        self.shape.push(word.to_string());
    }

    fn slot(&mut self, kind: &'static str, value: &str, prefix: &str) {
        let count = self.counts.entry(kind).or_insert(0);
        *count += 1;
        self.slots.insert(format!("{kind}{count}"), text(value));
        self.shape.push(format!("{prefix}{{{kind}}}"));
    }

    fn word(&mut self, word: &str, prefix: &str) {
        match kind(word) {
            None => self.literal(&format!("{prefix}{word}")),
            Some(kind) => self.slot(kind, word, prefix),
        }
    }
}

fn interpreter(word: &str) -> bool {
    let base = last_part(word);
    SCRIPT.contains(&base) || NUMBERED_PYTHON.is_match(base)
}

/// The shape of one simple command, or why it has to stay exact.
fn shape_words(words: &[String]) -> Result<Shaper, &'static str> {
    let mut shaper = Shaper::default();
    let (wrappers, words) = words.split_at(peel(words, true));
    for word in wrappers {
        if let Some(found) = ENV.captures(word) {
            shaper.slot("env", &found[2], &format!("{}=", &found[1]));
        } else if word.starts_with('-') && !NUMBER.is_match(word) {
            if !FLAG.is_match(word) {
                return Err("unsafe_flag");
            }
            shaper.literal(word);
        } else {
            shaper.word(word, "");
        }
    }
    let Some((program, rest)) = words.split_first() else {
        return Err("malformed");
    };
    let safe = if program.contains('/') {
        &SAFE_PATH
    } else {
        &PROGRAM
    };
    if !safe.is_match(program) {
        return Err("unsafe_program");
    }
    shaper.literal(program);
    let mut interprets = interpreter(program);
    let mut script_named = false;
    let mut i = 0;
    while i < rest.len() {
        let word = rest[i].as_str();
        if word.starts_with('-') && word != "-" && !NUMBER.is_match(word) {
            if interprets && !script_named && INLINE.contains(&word) {
                return Err("inline_code");
            }
            if let (true, Some(module)) =
                (interprets && !script_named && word == "-m", rest.get(i + 1))
            {
                if !MODULE.is_match(module) {
                    return Err("unsafe_program");
                }
                shaper.literal(word);
                shaper.literal(module);
                script_named = true;
                i += 2;
                continue;
            }
            if let Some(found) = LONG_WITH_VALUE.captures(word) {
                shaper.word(&found[2], &format!("{}=", &found[1]));
            } else if let Some(found) = SHORT_WITH_NUMBER.captures(word) {
                shaper.literal(&found[1]);
                shaper.slot("num", &found[2], "");
            } else if FLAG.is_match(word) || word == "--" {
                shaper.literal(word);
            } else {
                return Err("unsafe_flag");
            }
        } else if interprets && !script_named {
            // The script an interpreter runs is what the call does, not a value of it.
            if !SAFE_PATH.is_match(word) {
                return Err("unsafe_program");
            }
            shaper.literal(word);
            script_named = true;
        } else if word.chars().count() > 240 {
            return Err("long_value");
        } else if interpreter(word) && kind(word).is_none() {
            // `uv run python x.py`: the script after an interpreter that is run by another
            // program is what the call does too.
            shaper.literal(word);
            interprets = true;
            script_named = false;
        } else {
            shaper.word(word, "");
        }
        i += 1;
    }
    if shaper.shape.join(" ").chars().count() > MAX_SHAPE || shaper.slots.len() > 16 {
        return Err("long_shape");
    }
    Ok(shaper)
}

// ---- cutting a command into parts ----------------------------------------------------------

fn protect(sign: char) -> char {
    let found = PROTECTED.iter().find(|(plain, _)| *plain == sign);
    found.map_or(sign, |(_, written)| *written)
}

/// A word as it was, where a substitution in it was kept whole.
fn unprotect(word: &str) -> String {
    let back = |sign: char| {
        let found = PROTECTED.iter().find(|(_, written)| *written == sign);
        found.map_or(sign, |(plain, _)| *plain)
    };
    word.chars().map(back).collect()
}

/// A text as it was written: its redirections as `<` and `>` again, its substitutions too.
fn unmark(marked: &str) -> String {
    unprotect(&marked.replace(IN, "<").replace(OUT, ">"))
}

/// Where the `$(...)` that begins at `start` ends, when it only reads.
fn substitution_end(signs: &[char], start: usize) -> Option<usize> {
    let (mut quote, mut i) = (None::<char>, start + 2);
    loop {
        let sign = *signs.get(i)?;
        if let Some(open) = quote {
            if sign == open {
                quote = None;
            }
        } else if sign == '\'' || sign == '"' {
            quote = Some(sign);
        } else if sign == '(' {
            return None;
        } else if sign == ')' {
            break;
        }
        i += 1;
    }
    let inner: String = signs[start + 2..i].iter().collect();
    if python::strip(&inner).is_empty() || NOT_ONLY_READING.is_match(&inner) {
        return None;
    }
    for (index, segment) in inner.split('|').enumerate() {
        let words = tools::shell_split(segment)?;
        let base = words.first().map_or("", |word| last_part(word));
        let allowed = if index == 0 { SUBSTITUTE } else { FILTERS };
        let asks_git = index == 0
            && base == "git"
            && words
                .get(1)
                .is_some_and(|word| GIT_READ.contains(&word.as_str()));
        if !allowed.contains(&base) && !asks_git {
            return None;
        }
        let edits = base == "sed"
            && words
                .iter()
                .any(|word| word.starts_with("-i") || word == "--in-place");
        let sets = base == "date"
            && words
                .iter()
                .any(|word| word.starts_with("-s") || word.starts_with("--set"));
        if edits || sets {
            return None;
        }
    }
    Some(i + 1)
}

/// A command cut at the shell's operators outside of quotes: each part with the operator
/// before it and whether it redirects (its `<` and `>` are then written as [`IN`] and
/// [`OUT`]). `None`: it substitutes more than a reading, groups, or reads a heredoc, and is
/// told as one piece.
pub fn split_shell(command: &str) -> Option<Vec<(String, String, bool)>> {
    let signs: Vec<char> = command.chars().collect();
    let n = signs.len();
    let begins = |at: usize, with: &str| {
        let with: Vec<char> = with.chars().collect();
        n >= at + with.len() && signs[at..at + with.len()] == with[..]
    };
    let mut parts: Vec<(String, String, bool)> = Vec::new();
    let (mut current, mut quote, mut i) = (String::new(), None::<char>, 0);
    let (mut before, mut redirects) = (String::new(), false);
    while i < n {
        let sign = signs[i];
        if begins(i, "$(") && quote != Some('\'') {
            let end = substitution_end(&signs, i)?;
            current.extend(signs[i..end].iter().map(|sign| protect(*sign)));
            i = end;
            continue;
        }
        if let Some(open) = quote {
            if open == '"' && sign == '`' {
                return None;
            }
            current.push(sign);
            if sign == '\\' && open == '"' && i + 1 < n {
                current.push(signs[i + 1]);
                i += 1;
            } else if sign == open {
                quote = None;
            }
        } else if sign == '\'' || sign == '"' {
            quote = Some(sign);
            current.push(sign);
        } else if sign == '\\' && i + 1 < n {
            if signs[i + 1] == '\n' {
                // The line goes on.
                i += 2;
                continue;
            }
            current.push(sign);
            current.push(signs[i + 1]);
            i += 1;
        } else if matches!(sign, '`' | '(' | ')') || begins(i, "<<") {
            return None;
        } else if sign == '&'
            && ((i > 0 && matches!(signs[i - 1], '>' | '<')) || signs.get(i + 1) == Some(&'>'))
        {
            current.push(sign);
        } else if sign == '<' || sign == '>' {
            redirects = true;
            current.push(if sign == '<' { IN } else { OUT });
        } else if matches!(sign, '|' | '&' | ';' | '\n') {
            let two: String = signs[i..n.min(i + 2)].iter().collect();
            let operator = if ["&&", "||", "|&", ";;"].contains(&two.as_str()) {
                two
            } else {
                sign.to_string()
            };
            if operator == ";;" {
                return None;
            }
            i += operator.chars().count();
            let part = python::strip(&current).to_string();
            parts.push((part, std::mem::replace(&mut before, operator), redirects));
            redirects = false;
            current.clear();
            continue;
        } else {
            current.push(sign);
        }
        i += 1;
    }
    if quote.is_some() {
        return None;
    }
    parts.push((python::strip(&current).to_string(), before, redirects));
    parts.retain(|part| !part.0.is_empty());
    Some(parts)
}

/// A word of a part, or where it sends or takes what.
enum Shaped {
    Word(String),
    Redirect { operator: String, target: String },
}

/// The words of a part without the redirections that only drop or merge diagnostics; the
/// others as an operator and its target. Answers the words as the templates read them and
/// as the shape takes them. `None`: a redirection that cannot be read.
fn clean(words: &[String]) -> Option<(Vec<String>, Vec<Shaped>)> {
    let (mut plain, mut shaped, mut i) = (Vec::new(), Vec::new(), 0);
    while i < words.len() {
        let word = &words[i];
        i += 1;
        if DEVNULL.is_match(word) {
            continue;
        }
        let marked = word.contains(IN) || word.contains(OUT);
        let Some(found) = REDIRECT.captures(word).filter(|_| marked) else {
            if marked {
                return None;
            }
            plain.push(word.clone());
            shaped.push(Shaped::Word(word.clone()));
            continue;
        };
        let operator = unmark(&found[1]);
        let mut target = found[2].to_string();
        if target.is_empty() {
            target.clone_from(words.get(i)?);
            i += 1;
        }
        if target == "/dev/null" {
            continue;
        }
        if target.starts_with('&') || target.contains(IN) || target.contains(OUT) {
            return None;
        }
        plain.push(operator.clone());
        plain.push(target.clone());
        shaped.push(Shaped::Redirect { operator, target });
    }
    Some((plain, shaped))
}

/// A part's text as whoever explains it sees it: each heredoc written the one way, and
/// what it holds withheld.
fn withheld(lifted: &str) -> String {
    let count = lifted.matches(HEREDOC).count();
    let amid = format!(" {WITHHELD_HEREDOC} ");
    let written = HEREDOC_AMID.replace_all(lifted, NoExpand(&amid));
    format!("{}{}", python::strip(&written), WITHHELD_BODY.repeat(count))
}

/// A command with its heredocs lifted out, and with what they hold withheld, when every
/// heredoc can be shaped: the one simple command that reads it only runs it as a script or
/// stores it in a file, and what it holds runs nothing itself.
fn lift(command: &str) -> Option<(String, String)> {
    let lifted = lift_heredocs(command)?;
    if lifted.bodies.is_empty() {
        return Some((lifted.text, command.to_string()));
    }
    if lifted
        .bodies
        .iter()
        .any(|(body, quoted)| expanding(body, *quoted))
    {
        return None;
    }
    for (segment, _, redirects) in split_shell(&lifted.text)? {
        if !segment.contains(HEREDOC) {
            continue;
        }
        let words: Vec<String> = tools::shell_split(&segment)?
            .iter()
            .map(|word| unprotect(word))
            .collect();
        let words = if redirects { clean(&words)?.0 } else { words };
        if heredoc_consumer(&words).is_empty() {
            return None;
        }
    }
    Some((lifted.text, lifted.withheld))
}

/// A command with what each of its heredocs holds withheld, when every heredoc can be
/// shaped; otherwise the command as it is. What is withheld is never kept, sent or said, so
/// what is said of the call holds for whatever a heredoc held.
pub fn redact(command: &str) -> String {
    if !command.contains("<<") {
        return command.to_string();
    }
    let Some(inner) = strip_wrapper(command) else {
        return command.to_string();
    };
    match lift(&inner) {
        Some((_, hidden)) if hidden != inner => hidden,
        _ => command.to_string(),
    }
}

fn bash(command: &str) -> Map<String, Value> {
    let mut activity = Map::new();
    activity.insert("name".to_string(), text("Bash"));
    activity.insert("command".to_string(), text(command));
    activity.insert("input".to_string(), json!({"command": command}));
    activity
}

/// What a part of a command was cut into: its text, the operator before it, its words as
/// the templates read them, and as the shape takes them.
struct Parsed {
    text: String,
    operator: String,
    plain: Vec<String>,
    shaped: Vec<Shaped>,
}

fn piped(operator: &str) -> bool {
    operator == "|" || operator == "|&"
}

fn shell_parts(command: &str, depth: usize, key: &str) -> Vec<Part> {
    let Some(command) = strip_wrapper(command) else {
        let whole = first_program(command);
        return vec![exact(bash(command), command, "malformed", &whole, "?", key)];
    };
    let command = command.as_str();
    let whole = first_program(command);
    let as_one = |reason: &str| vec![exact(bash(command), command, reason, &whole, "?", key)];
    let Some(segments) = lift(command).and_then(|(lifted, _)| split_shell(&lifted)) else {
        return as_one("opaque");
    };
    let mut parsed: Vec<Parsed> = Vec::new();
    for (marked, operator, redirects) in segments {
        let Some(words) = tools::shell_split(&marked) else {
            return as_one("malformed");
        };
        let words: Vec<String> = words.iter().map(|word| unprotect(word)).collect();
        let cleaned = if redirects {
            clean(&words)
        } else {
            let shaped = words.iter().cloned().map(Shaped::Word).collect();
            Some((words, shaped))
        };
        let Some((plain, shaped)) = cleaned.filter(|(plain, _)| !plain.is_empty()) else {
            return as_one("opaque");
        };
        let first = plain.iter().find(|word| !ENV.is_match(word));
        let first = first.map_or("", String::as_str);
        if KEYWORDS.contains(&first) || KEYWORDS.contains(&plain[0].as_str()) {
            return as_one("opaque");
        }
        if piped(&operator) && !FILTERS.contains(&last_part(first)) {
            return as_one("opaque");
        }
        parsed.push(Parsed {
            text: unmark(&marked),
            operator,
            plain,
            shaped,
        });
    }
    let mut parts: Vec<Part> = Vec::new();
    let mut cwd = String::new();
    for (index, part) in parsed.iter().enumerate() {
        let plain = &part.plain;
        let base = last_part(&plain[0]);
        if parsed.len() > 1 && base == "cd" && plain.len() <= 2 {
            cwd = plain.get(1).cloned().unwrap_or_default();
            continue;
        }
        if parsed.len() > 1 && NOOP.contains(&base) && plain.len() == 1 {
            continue;
        }
        if piped(&part.operator) && SHORTENED.is_match(&plain.join(" ")) {
            continue;
        }
        let into_another = parsed
            .get(index + 1)
            .is_some_and(|next| piped(&next.operator));
        let a_shell = ["bash", "sh", "zsh"].contains(&base);
        if depth < 2
            && !into_another
            && !piped(&part.operator)
            && plain.len() == 3
            && part.shaped.len() == 3
            && a_shell
            && (plain[1] == "-c" || plain[1] == "-lc")
        {
            // `bash -c '...'` runs what it is given: that is explained in its place.
            for mut inner in shell_parts(&plain[2], depth + 1, key) {
                if inner.cwd.is_empty() {
                    inner.cwd.clone_from(&cwd);
                }
                parts.push(inner);
            }
            continue;
        }
        parts.push(shell_part(&part.text, &part.shaped, &cwd, key));
    }
    if parts.is_empty() {
        // Every part was a `cd` or did nothing: the first is told. A command that has no
        // word at all (a comment alone, a shell started for no script) is told as one piece;
        // the Python Host fails on it (defect R39).
        return match parsed.first() {
            Some(first) => vec![shell_part(&first.text, &first.shaped, "", key)],
            None => as_one("opaque"),
        };
    }
    if parts.len() > MAX_PARTS {
        return as_one("too_many_parts");
    }
    parts
}

fn shell_part(marked: &str, shaped: &[Shaped], cwd: &str, key: &str) -> Part {
    let shown = if marked.contains(HEREDOC) {
        withheld(marked)
    } else {
        marked.to_string()
    };
    let activity = bash(&shown);
    let words: Vec<String> = shaped
        .iter()
        .filter_map(|entry| match entry {
            Shaped::Word(word) if word != HEREDOC => Some(word.clone()),
            _ => None,
        })
        .collect();
    let (runs, first) = program(&words);
    let mut shaper = match shape_words(&words) {
        Ok(shaper) => shaper,
        Err(reason) => {
            let mut part = exact(activity, &shown, reason, &runs, &first, key);
            part.cwd = cwd.to_string();
            return part;
        }
    };
    for entry in shaped {
        match entry {
            Shaped::Redirect { operator, target } => {
                shaper.slot("path", target, &format!("{operator} "));
            }
            // What a heredoc holds is no part of the shape, and no slot.
            Shaped::Word(word) if word == HEREDOC => shaper.literal("<<heredoc"),
            Shaped::Word(_) => {}
        }
    }
    let shape = identity(key, "shape", &json!(shaper.shape));
    let whole = identity(key, "exact", &text(&shown));
    let signature = format!("sh:{runs} #{shape}");
    let exact_key = format!("x:{runs} #{whole}");
    let legacy = if first == runs {
        Vec::new()
    } else {
        vec![
            (format!("sh:{first} #{shape}"), signature.clone()),
            (format!("x:{first} #{whole}"), exact_key.clone()),
        ]
    };
    Part {
        activity,
        signature,
        exact_key,
        program: runs,
        slots: shaper.slots,
        exact: false,
        reason: String::new(),
        cwd: cwd.to_string(),
        legacy,
    }
}

// ---- other tools ---------------------------------------------------------------------------

/// A use as Python shows it sorted by its fields (`repr(sorted(activity.items()))`): what
/// an exact part of a named tool is known by.
fn shown_sorted(activity: &Map<String, Value>) -> String {
    let mut fields: Vec<(&String, &Value)> = activity
        .iter()
        .filter(|(key, _)| *key != "scripts")
        .collect();
    fields.sort_by(|a, b| a.0.cmp(b.0));
    let fields: Vec<String> = fields
        .iter()
        .map(|(key, value)| format!("({}, {})", python::text(key), python::repr(value)))
        .collect();
    format!("[{}]", fields.join(", "))
}

fn tool_part(activity: &Map<String, Value>, key: &str) -> Part {
    let name = said_of(activity.get("name"));
    let tool_kind = said_of(activity.get("kind"));
    let called = if name.is_empty() { &tool_kind } else { &name };
    let program = format!("tool:{called}");
    let program = python::first(&program, 120);
    let whole = shown_sorted(activity);
    if name.is_empty() && tool_kind.is_empty() {
        return exact(activity.clone(), &whole, "malformed", "?", "?", key);
    }
    let opaque = || {
        exact(
            activity.clone(),
            &whole,
            "opaque_input",
            program,
            program,
            key,
        )
    };
    let inputs = activity.get("input").and_then(Value::as_object);
    let given = |under: &str| inputs.and_then(|inputs| inputs.get(under));
    let runs = ["code", "command", "cmd"]
        .iter()
        .any(|under| given(under).is_some_and(python::truthy));
    if activity.get("input").is_some_and(Value::is_string) || runs {
        return opaque();
    }
    let mut shaper = Shaper::default();
    shaper.literal(&name);
    shaper.literal(&tool_kind);
    let operations = activity.get("operations").and_then(Value::as_array);
    for operation in operations.into_iter().flatten().filter_map(Value::as_str) {
        let (label, target) = operation.split_once(": ").unwrap_or((operation, ""));
        if target.is_empty() || !WORD.is_match(&label.to_lowercase()) {
            return opaque();
        }
        shaper.literal(label);
        shaper.slot("path", target, "");
    }
    // What the use says of itself, and over it what it was given: by the fields' names.
    let mut fields: BTreeMap<&str, &str> = BTreeMap::new();
    for source in [Some(activity), inputs].into_iter().flatten() {
        for name in TOOL_FIELDS {
            if let Some(value) = source.get(*name).and_then(Value::as_str) {
                fields.insert(*name, value);
            }
        }
    }
    for (name, value) in fields {
        let free = ["title", "summary", "description", "query", "pattern"].contains(&name);
        shaper.literal(&format!("{name}="));
        match if free { Some("text") } else { kind(value) } {
            None => shaper.literal(value),
            Some(kind) => shaper.slot(kind, value, ""),
        }
    }
    let shape = identity(key, "tool-shape", &json!(shaper.shape));
    Part {
        activity: activity.clone(),
        signature: format!("tool:{called} #{shape}"),
        exact_key: format!("x:{program} #{}", identity(key, "exact", &text(&whole))),
        program: program.to_string(),
        slots: shaper.slots,
        exact: false,
        reason: String::new(),
        cwd: String::new(),
        legacy: Vec::new(),
    }
}

const SHELL_TOOLS: [&str; 4] = ["bash", "shell", "exec_command", "local_shell"];

fn shell_command<'a>(activity: &'a Map<String, Value>, tool_kind: &str) -> &'a str {
    match field(activity, &["command", "cmd"]) {
        "" if tool_kind == "command" => activity
            .get("summary")
            .and_then(Value::as_str)
            .map(python::strip)
            .unwrap_or_default(),
        command => command,
    }
}

/// A shell tool's name or description alone says nothing about what it runs (R184).
pub fn lacks_command(activity: &Map<String, Value>) -> bool {
    let name = said_of(activity.get("name"));
    let name = python::strip(&name).to_lowercase();
    let kind = said_of(activity.get("kind"));
    let kind = python::strip(&kind);
    if (!SHELL_TOOLS.contains(&name.as_str()) && kind != "command")
        || !shell_label(activity).is_empty()
    {
        return false;
    }
    let command = shell_command(activity, kind).to_lowercase();
    command.is_empty() || SHELL_TOOLS.contains(&command.as_str()) || command == name
}

/// The parts of a use of a tool, as it was read. `key`: the Host's own, under which a
/// shape is signed.
pub fn split(activity: &Map<String, Value>, key: &str) -> Vec<Part> {
    if lacks_command(activity) {
        return Vec::new();
    }
    let name = said_of(activity.get("name"));
    let name = python::strip(&name).to_lowercase();
    let tool_kind = said_of(activity.get("kind"));
    let tool_kind = python::strip(&tool_kind);
    let command = shell_command(activity, tool_kind);
    if (SHELL_TOOLS.contains(&name.as_str()) || tool_kind == "command") && !command.is_empty() {
        return shell_parts(command, 0, key);
    }
    let label = shell_label(activity);
    if label.is_empty() {
        return vec![tool_part(activity, key)];
    }
    if label.chars().count() >= LABEL_CLIP {
        // A name that was cut off is not the whole command: it is only ever told as itself.
        let whole = first_program(&label);
        return vec![exact(
            activity.clone(),
            &label,
            "truncated",
            &whole,
            "?",
            key,
        )];
    }
    shell_parts(&label, 0, key)
}

/// A part bound to the scripts it was explained with. What a script holds is part of what
/// the call does, so a script that changed makes another signature; only a digest of what
/// was read of the scripts is kept in it.
pub fn with_scripts(mut part: Part, scripts: &[Value]) -> Part {
    if scripts.is_empty() {
        return part;
    }
    let excerpts: Vec<Value> = scripts
        .iter()
        .map(|script| {
            script
                .get("source_excerpt")
                .cloned()
                .unwrap_or_else(|| text(""))
        })
        .collect();
    let digest: String = Sha256::digest(python::repr(&Value::Array(excerpts)).as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let mark = format!("~{}", &digest[..12]);
    part.activity
        .insert("scripts".to_string(), Value::Array(scripts.to_vec()));
    part.signature.push_str(&mark);
    if part.exact {
        part.exact_key.clone_from(&part.signature);
    } else {
        part.exact_key.push_str(&mark);
    }
    for (old, new) in &mut part.legacy {
        old.push_str(&mark);
        new.push_str(&mark);
    }
    part
}

// ---- sentences with places for a call's values ---------------------------------------------

/// What a call has in its slots, and what follows from it: the last part of a path
/// (`path1_name`), the host of an address (`url1_host`).
pub fn slot_values(slots: &Map<String, Value>) -> Map<String, Value> {
    let mut values = slots.clone();
    for (name, value) in slots {
        let Some(value) = value.as_str() else {
            continue;
        };
        if name.starts_with("path") {
            let base = last_part(value.trim_end_matches('/'));
            if !base.is_empty() && base != value && base != ".." && base != "~" {
                values.insert(format!("{name}_name"), text(base));
            }
        } else if name.starts_with("url") {
            let host = host_of(value);
            if !host.is_empty() {
                values.insert(format!("{name}_host"), text(&host));
            }
        }
    }
    values
}

/// The places a sentence has for values, each once, in the order they stand.
pub fn placeholders(template: &str) -> Vec<&str> {
    let mut found: Vec<&str> = Vec::new();
    for place in PLACEHOLDER.captures_iter(template) {
        let name = place.get(1).map_or("", |name| name.as_str());
        if !found.contains(&name) {
            found.push(name);
        }
    }
    found
}

/// A sentence with its places filled with a call's values. `None`: a place has no value
/// in this call.
pub fn render(template: &str, slots: &Map<String, Value>) -> Option<String> {
    let values = slot_values(slots);
    let mut said = String::new();
    let mut from = 0;
    for place in PLACEHOLDER.captures_iter(template) {
        let (whole, name) = (place.get(0)?, place.get(1)?);
        said.push_str(&template[from..whole.start()]);
        said.push_str(values.get(name.as_str())?.as_str()?);
        from = whole.end();
    }
    said.push_str(&template[from..]);
    Some(said)
}

/// Whether a sentence with places may be said of other values than this call's: the
/// sentence itself, or why not. It has to come out as exactly what was said of this call,
/// and may repeat none of this call's values outside a place, nor a number where the call
/// has numbers: either would be wrong for the next call of the same shape.
pub fn parameterise<'a>(
    said: &str,
    template: Option<&'a str>,
    slots: &Map<String, Value>,
) -> Result<&'a str, &'static str> {
    let Some(template) = template.filter(|template| !python::strip(template).is_empty()) else {
        return Err("no_template");
    };
    let values = slot_values(slots);
    if placeholders(template)
        .iter()
        .any(|place| !values.contains_key(*place))
    {
        return Err("unknown_slot");
    }
    if render(template, slots).as_deref() != Some(said) {
        return Err("mismatch");
    }
    let bare = PLACEHOLDER.replace_all(template, "").to_lowercase();
    let repeated = |value: &str| value.chars().count() >= 2 && bare.contains(&value.to_lowercase());
    if values.values().filter_map(Value::as_str).any(repeated) {
        return Err("literal_value");
    }
    let counts = slots.keys().any(|name| name.starts_with("num"));
    if counts && (DIGIT.is_match(&bare) || NUMBER_WORDS.is_match(&bare)) {
        return Err("literal_number");
    }
    let fixed = PLACEHOLDER.replace_all(template, " ");
    if specific_literal(&fixed) {
        return Err("specific_literal");
    }
    Ok(template)
}

static FIXED_WORD: LazyLock<Regex> = LazyLock::new(|| pattern(r#"[^\s\x1c-\x1f,;:()\[\]{}"'`]+"#));
static DOTTED: LazyLock<Regex> = LazyLock::new(|| pattern(r"[A-Za-z0-9]\.[A-Za-z0-9]"));
static IDENTIFIER: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+|\d+"));

fn specific_literal(fixed: &str) -> bool {
    FIXED_WORD.find_iter(fixed).any(|word| {
        let word = word.as_str().trim_end_matches(['.', '!', '?']);
        word.contains('/')
            || word.starts_with('~')
            || DIGIT.is_match(word)
            || word.contains('_')
            || DOTTED.is_match(word)
            || word.matches('-').nth(1).is_some()
    })
}

/// An older reusable sentence can carry values that belong to another call.
pub(super) fn foreign(template: &str, evidence: &str) -> bool {
    let fixed = PLACEHOLDER.replace_all(template, " ");
    let evidence = evidence.to_lowercase();
    let bytes = evidence.as_bytes();
    IDENTIFIER.find_iter(&fixed).any(|word| {
        let word = word.as_str();
        let specific =
            DIGIT.is_match(word) || word.matches('-').nth(1).is_some() || word.contains(['_', '.']);
        let word = word.to_lowercase();
        specific
            && !evidence.match_indices(word.as_str()).any(|(at, token)| {
                let before = at.checked_sub(1).and_then(|at| bytes.get(at));
                let after = bytes.get(at + token.len());
                !before.is_some_and(u8::is_ascii_alphanumeric)
                    && !after.is_some_and(u8::is_ascii_alphanumeric)
            })
    })
}

/// One sentence of a call from what was said of its parts, in their order.
pub fn join(said: &[&str]) -> String {
    let mut unique: Vec<&str> = Vec::new();
    for sentence in said.iter().map(|sentence| python::strip(sentence)) {
        if !sentence.is_empty() && unique.last() != Some(&sentence) {
            unique.push(sentence);
        }
    }
    let mut sentences: Vec<String> = Vec::new();
    for (place, sentence) in unique.iter().enumerate() {
        if place == 0 {
            sentences.push((*sentence).to_string());
            continue;
        }
        let mut signs = sentence.chars();
        let first = signs.next();
        // A sentence goes on in small letters, but a name or a word in capitals stays.
        let goes_on = signs.next().is_some_and(char::is_lowercase);
        let rest = sentence
            .get(first.map_or(0, char::len_utf8)..)
            .unwrap_or_default();
        match first.filter(|_| goes_on) {
            Some(first) => sentences.push(format!("Then {}{rest}", first.to_lowercase())),
            None => sentences.push(format!("Then {sentence}")),
        }
    }
    let mut joined = sentences.join(" ");
    while joined.chars().count() > 240 && sentences.len() > 1 {
        sentences.pop();
        let left = unique.len() - sentences.len();
        let steps = if left == 1 { "step" } else { "steps" };
        joined = format!("{} Then {left} more {steps}.", sentences.join(" "));
    }
    python::first(&joined, 240).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_word_is_given_to_a_shell_as_python_quotes_it() {
        for (word, quoted) in [
            ("", "''"),
            ("src", "src"),
            ("/srv/app-1/a_b.c", "/srv/app-1/a_b.c"),
            ("my dir", "'my dir'"),
            ("it's", "'it'\"'\"'s'"),
            ("a$b", "'a$b'"),
            ("\u{e9}t\u{e9}", "'\u{e9}t\u{e9}'"),
        ] {
            assert_eq!(shell_quote(word), quoted, "{word}");
        }
    }

    #[test]
    fn a_part_is_read_back_as_it_was_written() {
        let parts = split(&bash("cd /srv && timeout 5 cat a.txt > out.txt"), "a key");
        assert_eq!(parts.len(), 1);
        let part = &parts[0];
        assert_eq!((part.cwd.as_str(), part.program.as_str()), ("/srv", "cat"));
        assert_eq!(part.legacy.len(), 2);
        assert_eq!(&Part::from_json(&part.to_json()), part);
    }

    #[test]
    fn a_command_of_no_word_is_told_as_one_piece() {
        for command in [
            "# only a comment",
            "bash -c ''",
            ";",
            "&&",
            "|",
            "   # indented",
            "bash -lc '# z'",
            "sh -c ';'",
        ] {
            let parts = split(&bash(command), "a key");
            assert_eq!(parts.len(), 1, "{command}");
            assert!(parts[0].exact, "{command}");
            assert_eq!(parts[0].reason, "opaque", "{command}");
        }
    }
}
