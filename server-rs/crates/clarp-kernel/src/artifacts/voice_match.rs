//! What a spoken answer to a request means: an option, a yes or a no, a command, or the
//! user's own words.
//!
//! The user holds a microphone on a request and says the answer ("velg B", "ja", "neste").
//! The app transcribes it and asks the Host what it means; nothing is changed by asking.
//! Only a clear answer is read as an option or a yes or no: anything more than that ("ja,
//! men vent til i morgen") is the user's own words, sent as they were said. The shared vectors
//! are `contract/fixtures/decision-voice-match.json`; apps read them, and never match by
//! themselves.

use serde_json::{Value, json};
use unicode_normalization::UnicodeNormalization;

use super::decisions::Decision;

/// The command words an app sends from its own catalog, in its user's language: what a
/// prompt tells the user to say is what works. Matched before the Host's own words.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Commands {
    pub next: Vec<String>,
    pub undo: Vec<String>,
    pub cancel: Vec<String>,
}

impl Commands {
    /// The words of a request's `commands` (`{"next": [...], "undo": [...], "cancel":
    /// [...]}`); what is not a list of texts says nothing.
    pub fn of(value: Option<&Value>) -> Self {
        let words = |name: &str| -> Vec<String> {
            value
                .and_then(|value| value.get(name))
                .and_then(Value::as_array)
                .map(|list| {
                    list.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default()
        };
        Self {
            next: words("next"),
            undo: words("undo"),
            cancel: words("cancel"),
        }
    }
}

const NEXT: &[&str] = &["neste", "hopp over", "next", "skip"];
const UNDO: &[&str] = &["angre", "undo"];
const CANCEL: &[&str] = &["avbryt", "cancel"];

/// Sounds that say nothing, wherever they are.
const FILLER: &[&str] = &["eh", "ehm", "øh", "øhm", "um", "uh", "uhm", "hmm", "mm", "erm"];

/// Openings that say nothing before what is said.
const FILLER_OPENINGS: &[&str] = &["ok så", "okei så", "okay so", "ok so"];

/// Words that lead into a choice ("velg B", "option two"), the longest first.
const LEAD_INS: &[&str] = &[
    "jeg velger",
    "jeg tar",
    "i choose",
    "i pick",
    "velg",
    "svar",
    "svaret er",
    "alternativ",
    "alternativet",
    "valg",
    "nummer",
    "choose",
    "pick",
    "option",
    "number",
    "answer",
    "the",
    "den",
];

/// Words that may end a choice without saying more ("the first one", "B takk").
const TRAILERS: &[&str] = &[
    "one", "please", "takk", "thanks", "option", "alternativet", "da",
];

/// Words that may stand around an option's label without making the answer more than it.
const CARRIERS: &[&str] = &[
    "please", "thanks", "takk", "the", "one", "den", "det", "jeg", "i", "will", "want", "vil",
    "ha", "take", "tar", "go", "with", "med", "går", "option", "alternativet", "alternativ",
    "choose", "velger", "pick", "velg", "lets", "let's", "la", "oss", "ta", "gå",
];

/// Letters, as written and as their names are said in Norwegian and English.
const LETTERS: &[(&str, usize)] = &[
    ("a", 0),
    ("b", 1),
    ("be", 1),
    ("bee", 1),
    ("c", 2),
    ("se", 2),
    ("see", 2),
    ("ce", 2),
    ("d", 3),
    ("de", 3),
    ("dee", 3),
    ("e", 4),
];

/// Ordinals and numbers.
const NUMBERS: &[(&str, usize)] = &[
    ("første", 0),
    ("først", 0),
    ("en", 0),
    ("ett", 0),
    ("one", 0),
    ("first", 0),
    ("1", 0),
    ("andre", 1),
    ("to", 1),
    ("two", 1),
    ("second", 1),
    ("2", 1),
    ("tredje", 2),
    ("tre", 2),
    ("three", 2),
    ("third", 2),
    ("3", 2),
    ("fjerde", 3),
    ("fire", 3),
    ("four", 3),
    ("fourth", 3),
    ("4", 3),
];

const YES: &[&str] = &[
    "ja", "jepp", "jo", "jada", "ja takk", "ok", "okei", "okay", "godkjenn", "kjør", "yes",
    "yep", "yeah", "yes please", "sure", "approve", "go ahead", "sí", "si", "oui", "हाँ", "हां",
    "是", "好",
];

const NO: &[&str] = &[
    "nei", "nope", "nei takk", "avvis", "stopp", "no", "no thanks", "reject", "deny", "non",
    "नहीं", "不",
];

/// Folds a letter to its plain form: accents go, but æ, ø and å are letters of their own.
fn folded(text: &str) -> String {
    let mut out = String::new();
    for sign in text.chars() {
        if matches!(sign, 'å' | 'æ' | 'ø') {
            out.push(sign);
            continue;
        }
        out.extend(
            sign.to_string()
                .nfd()
                .filter(|part| !('\u{0300}'..='\u{036f}').contains(part)),
        );
    }
    out
}

/// Whether a sign only separates words.
fn separates(sign: char) -> bool {
    sign.is_whitespace()
        || sign.is_ascii_punctuation() && sign != '\''
        || "«»“”‘’„…–—¡¿。，、！？：；·".contains(sign)
}

/// The words of a text as they are compared: lower case, accents folded, without punctuation.
fn words(text: &str) -> Vec<String> {
    folded(&text.to_lowercase())
        .split(separates)
        .filter(|word| !word.is_empty())
        .map(str::to_string)
        .collect()
}

/// Whether `words` starts with the words of `phrase`.
fn starts_with(words: &[String], phrase: &str) -> bool {
    let phrase: Vec<&str> = phrase.split(' ').collect();
    words.len() >= phrase.len() && words.iter().zip(&phrase).all(|(a, b)| a == b)
}

/// The words without what says nothing: filler sounds, and an opening that says nothing.
fn without_filler(mut said: Vec<String>) -> Vec<String> {
    said.retain(|word| !FILLER.contains(&word.as_str()));
    while let Some(opening) = FILLER_OPENINGS.iter().find(|o| starts_with(&said, o)) {
        said.drain(..opening.split(' ').count());
    }
    said
}

/// The words without what leads into a choice.
fn without_lead_ins(mut said: Vec<String>) -> Vec<String> {
    while let Some(lead) = LEAD_INS
        .iter()
        .filter(|lead| starts_with(&said, lead) && said.len() > lead.split(' ').count())
        .max_by_key(|lead| lead.split(' ').count())
    {
        said.drain(..lead.split(' ').count());
    }
    said
}

/// The words of a choice without what leads into it or merely ends it.
fn plain_choice(said: Vec<String>) -> Vec<String> {
    let mut said = without_lead_ins(said);
    while said.len() > 1
        && said
            .last()
            .is_some_and(|word| TRAILERS.contains(&word.as_str()))
    {
        said.pop();
    }
    said
}

fn joined(words: &[String]) -> String {
    words.join(" ")
}

/// Whether the whole of what was said is one of `phrases`, compared as words.
fn is_one_of<S: AsRef<str>>(said: &str, phrases: &[S]) -> bool {
    !said.is_empty() && phrases.iter().any(|phrase| joined(&words(phrase.as_ref())) == said)
}

/// The option a single word names by its letter or its number, where it names one there is.
fn by_position(word: &str, count: usize) -> Option<usize> {
    let index = LETTERS
        .iter()
        .chain(NUMBERS)
        .find(|(name, _)| *name == word)
        .map(|(_, index)| *index)?;
    (index < count).then_some(index)
}

/// The option whose label the words are: equal to it, a prefix of only it, or holding only it
/// with nothing else of substance.
fn by_label(said: &[String], labels: &[Vec<String>]) -> Option<usize> {
    let text = joined(said);
    if text.is_empty() {
        return None;
    }
    let only = |found: Vec<usize>| (found.len() == 1).then(|| found[0]);
    if let Some(index) = only(
        (0..labels.len())
            .filter(|&i| joined(&labels[i]) == text)
            .collect(),
    ) {
        return Some(index);
    }
    if text.chars().count() >= 3
        && let Some(index) = only(
            (0..labels.len())
                .filter(|&i| joined(&labels[i]).starts_with(&text))
                .collect(),
        )
    {
        return Some(index);
    }
    let holds = |label: &[String]| -> bool {
        if label.is_empty() || label.len() > said.len() {
            return false;
        }
        (0..=said.len() - label.len()).any(|start| {
            said[start..start + label.len()] == *label
                && said[..start]
                    .iter()
                    .chain(&said[start + label.len()..])
                    .all(|word| CARRIERS.contains(&word.as_str()))
        })
    };
    only((0..labels.len()).filter(|&i| holds(&labels[i])).collect())
}

fn custom(transcript: &str) -> Value {
    let said = transcript.split_whitespace().collect::<Vec<_>>().join(" ");
    json!({ "kind": "custom", "text": said })
}

/// What a spoken answer to `decision` means. `transcript` is what the app heard.
pub fn voice_match(decision: &Decision, transcript: &str, commands: &Commands) -> Value {
    let said = without_filler(words(transcript));
    if said.is_empty() {
        return json!({ "kind": "custom", "text": "" });
    }
    let text = joined(&said);
    let named = [
        ("next", &commands.next, NEXT),
        ("undo", &commands.undo, UNDO),
        ("cancel", &commands.cancel, CANCEL),
    ];
    // The app's own words first: what its prompt told the user to say.
    for (command, own, _) in &named {
        if is_one_of(&text, own) {
            return json!({ "kind": "command", "command": command });
        }
    }
    for (command, _, built_in) in &named {
        if is_one_of(&text, built_in) {
            return json!({ "kind": "command", "command": command });
        }
    }
    match decision.response_type.as_str() {
        "approval" => {
            let plain = joined(&plain_choice(said));
            let answers = |label: &str, list: &[&str]| {
                is_one_of(&text, list) || is_one_of(&plain, list) || is_one_of(&text, &[label])
            };
            if answers(&decision.yes_label, YES) {
                json!({ "kind": "approval", "choice": "accepted" })
            } else if answers(&decision.no_label, NO) {
                json!({ "kind": "approval", "choice": "rejected" })
            } else {
                custom(transcript)
            }
        }
        "single_choice" => {
            let labels: Vec<Vec<String>> = decision
                .options
                .iter()
                .map(|option| words(option["label"].as_str().unwrap_or_default()))
                .collect();
            let count = labels.len();
            let chosen = by_label(&said, &labels).or_else(|| {
                let plain = plain_choice(said.clone());
                match plain.as_slice() {
                    [word] => by_position(word, count).or_else(|| by_label(&plain, &labels)),
                    // A letter followed by its own label: "B, Pebble".
                    [word, rest @ ..] => by_position(word, count)
                        .filter(|&index| by_label(rest, &labels) == Some(index))
                        .or_else(|| by_label(&plain, &labels)),
                    [] => None,
                }
            });
            match chosen {
                Some(index) => {
                    let option = &decision.options[index];
                    json!({
                        "kind": "option",
                        "option_id": option["id"],
                        "label": option["label"],
                    })
                }
                None => custom(transcript),
            }
        }
        _ => custom(transcript),
    }
}
