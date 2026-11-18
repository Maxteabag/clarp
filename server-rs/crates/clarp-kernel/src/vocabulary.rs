//! Selection, ranking and rendering of transcription vocabulary. The audit and
//! the prompt are built together so that the run records what the engine got.
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::LazyLock;

use crate::python;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Budget {
    pub provider: String,
    pub model: String,
    pub unit: String,
    pub capacity: i64,
    pub max_term_chars: Option<usize>,
    pub supports_prose: bool,
}

#[allow(clippy::expect_used)]
pub static BUDGETS: LazyLock<Vec<Budget>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("vocabulary-budgets.json")).expect("reference budget table")
});

pub fn budget_for(provider: &str, model: &str) -> Budget {
    let key = provider.trim().to_lowercase();
    let mut base = BUDGETS
        .iter()
        .find(|b| b.provider == key)
        .unwrap_or(&BUDGETS[0])
        .clone();
    if !model.is_empty() {
        base.model = model.to_string();
    }
    if key == "cartesia" && base.model.trim().eq_ignore_ascii_case("ink-whisper") {
        base.capacity = 0;
        base.max_term_chars = None;
    }
    base
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Term {
    pub text: String,
    pub pack: String,
    pub rarity: f64,
    pub recency: f64,
    pub confusable: bool,
    pub say_as: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Pack {
    pub name: String,
    pub terms: Vec<Term>,
    pub priority: f64,
    pub floor: i64,
    pub enabled: bool,
}

pub fn estimate_tokens(text: &str) -> usize {
    let mut lexical = 0;
    let mut in_word = false;
    for c in text.chars() {
        let word = c.is_alphanumeric() || c == '_';
        if (word && !in_word) || (!word && !python::blank(c)) {
            lexical += 1;
        }
        in_word = word;
    }
    lexical.max(text.len().div_ceil(3))
}

const COMMON: &str = "a about after all also am an and any are as at back be because been before being but by call can come could day did do does doing done down each even every first for from get give go going good got had has have he her here him his how i if in into is it its just know like little long look made make man many may me more most much must my new no not now of on one only or other our out over own people put said same say see she should so some take than that the their them then there these they thing think this those time to too two up us use very want was way we well went were what when where which while who why will with work would year you your agent agents session server model voice audio okay yeah yes no hmm uh um gonna kinda";
static COMMON_WORDS: LazyLock<HashSet<&str>> =
    LazyLock::new(|| COMMON.split_whitespace().collect());
#[allow(clippy::expect_used)]
static WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[A-Za-z][A-Za-z0-9'_-]*").expect("vocabulary word"));
#[allow(clippy::expect_used)]
static IDENTIFIER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:(?:[A-Z][a-z0-9]+){2,}|[a-z]+(?:_[a-z0-9]+)+)$").expect("identifier")
});

pub fn rarity(text: &str) -> f64 {
    if python::strip(text).is_empty() {
        return 0.0;
    }
    WORD.find_iter(text)
        .map(|m| {
            let word = m.as_str();
            if COMMON_WORDS.contains(word.to_ascii_lowercase().as_str()) {
                0.05
            } else if IDENTIFIER.is_match(word) {
                0.98
            } else if word.bytes().any(|b| b.is_ascii_digit()) {
                0.9
            } else if word.as_bytes()[0].is_ascii_uppercase() {
                0.85
            } else if word.len() >= 12 {
                0.7
            } else {
                0.55
            }
        })
        .reduce(f64::max)
        .unwrap_or(0.5)
}

fn rank(term: &Term, pack: &Pack) -> f64 {
    (term.recency * 2.0 + term.rarity * 1.5 + if term.confusable { 0.75 } else { 0.0 })
        * pack.priority.max(0.0)
}

struct Selection<'a> {
    budget: &'a Budget,
    floor: f64,
    seen: HashSet<String>,
    chosen: Vec<Term>,
    dropped: Vec<Value>,
    used: usize,
}

impl Selection<'_> {
    fn drop_term(&mut self, t: &Term, reason: &str) {
        self.dropped
            .push(json!({"text":t.text,"pack":t.pack,"reason":reason}));
    }
    fn admit(&mut self, t: &Term, guaranteed: bool) {
        let key = python::casefold(&t.text);
        if self.seen.contains(&key) {
            self.drop_term(t, "duplicate");
            return;
        }
        if !guaranteed && t.rarity < self.floor && !t.confusable {
            self.drop_term(t, "below rarity floor");
            return;
        }
        let mut fitted = t.clone();
        if let Some(max) = self.budget.max_term_chars.filter(|m| *m > 0)
            && t.text.chars().count() > max
        {
            fitted.text = python::first(&t.text, max)
                .trim_end_matches(python::blank)
                .into();
        }
        let cost = match self.budget.unit.as_str() {
            "tokens" => estimate_tokens(&fitted.text) + 1,
            "words" => fitted
                .text
                .split(python::blank)
                .filter(|s| !s.is_empty())
                .count(),
            _ => 1,
        };
        if self.used + cost > self.budget.capacity as usize {
            self.drop_term(t, "over budget");
            return;
        }
        self.used += cost;
        self.seen.insert(key);
        self.chosen.push(fitted);
    }
}

pub fn compile(packs: &[Pack], budget: &Budget) -> Value {
    let floor = match budget.capacity {
        ..=0 => 1.0,
        1..120 => 0.65,
        120..300 => 0.45,
        300..800 => 0.25,
        _ => 0.0,
    };
    let mut selection = Selection {
        budget,
        floor,
        seen: HashSet::new(),
        chosen: Vec::new(),
        dropped: Vec::new(),
        used: 0,
    };
    let active: Vec<_> = packs
        .iter()
        .filter(|p| p.enabled && !p.terms.is_empty())
        .collect();
    if budget.capacity <= 0 || active.is_empty() {
        for t in packs.iter().flat_map(|p| &p.terms) {
            selection.drop_term(t, "no budget");
        }
    } else {
        let mut floors = active.clone();
        floors.sort_by(|a, b| b.priority.total_cmp(&a.priority));
        for pack in floors {
            let mut ranked: Vec<_> = pack.terms.iter().collect();
            ranked.sort_by(|a, b| rank(b, pack).total_cmp(&rank(a, pack)));
            for t in ranked.into_iter().take(pack.floor.max(0) as usize) {
                selection.admit(t, true);
            }
        }
        let mut rest: Vec<_> = active
            .iter()
            .flat_map(|p| p.terms.iter().map(move |t| (rank(t, p), t)))
            .filter(|(_, t)| !selection.seen.contains(&python::casefold(&t.text)))
            .collect();
        rest.sort_by(|a, b| b.0.total_cmp(&a.0));
        for (_, t) in rest {
            selection.admit(t, false);
        }
    }
    let mut words: Vec<_> = selection.chosen.iter().map(|t| t.text.as_str()).collect();
    let (form, payload) = if words.is_empty() || budget.unit == "terms" {
        ("terms", words.join(", "))
    } else {
        words.reverse();
        let text = words.join(", ");
        if budget.supports_prose && budget.capacity >= 300 {
            ("prose", format!("Terms that may come up: {text}."))
        } else {
            ("terms", format!("{text}."))
        }
    };
    let included:Vec<_>=selection.chosen.iter().map(|t|json!({"text":t.text,"pack":t.pack,"rarity":format!("{:.3}",t.rarity).parse::<f64>().unwrap_or(t.rarity)})).collect();
    let capacity = budget.capacity.max(0) as usize;
    json!({"payload":payload,"form":form,"unit":budget.unit,"used":selection.used,"capacity":capacity,
        "headroom":capacity-selection.used,"rarity_floor":floor,"included":included,"dropped":selection.dropped})
}
pub mod sources;

#[allow(clippy::expect_used)]
pub static JUNK_QUESTION: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("vocabulary-judge.json")).expect("reference junk question")
});

pub fn judged_text(text: &str, probability: Option<f64>) -> String {
    match probability {
        None => crate::heard::kept(text),
        Some(value) if value >= 0.6 => String::new(),
        Some(_) => text.trim().into(),
    }
}
