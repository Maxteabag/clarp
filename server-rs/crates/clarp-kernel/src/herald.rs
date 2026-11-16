//! The herald: an agent that has something to say raises its hand.
//!
//! When an agent says something aloud and its user is not with that agent, the reply is
//! not played over whatever the user is doing. A short clip is played instead, "<name>
//! here, ready for an update.", and the reply is held until the user turns to that agent:
//! opens it, writes to it, or says yes and its name. A reply of a few words is played at
//! once. The user is with an agent when it is the open one; and for a minute after a
//! message, with the agent the message went to and with no other, not even the open one.
//!
//! This is the Python Host's rule (`server/lib/herald.py`, `server/lib/intent.py`), and it
//! is on there unless the owner turns it off. Here it is a machine without clocks, voices
//! or streams of its own: it is told what happens and says what is to be played. Making
//! the herald's clip takes an engine and a while, so a clip that raises a hand is taken in
//! two steps ([`Herald::ingest`], [`Herald::made`]), and what happens in between is taken
//! as it comes.
//!
//! Held to the Python Host's own manager and classifier by
//! `tests/fixtures/herald-vectors.json`.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::python;

/// How long after a message the agent it went to is the only one heard at once.
pub const AWAITING_MS: i64 = 60_000;
/// How many signs a reply may have to be said at once, unless the owner says otherwise.
pub const SHORT_USUAL: i64 = 120;
/// The most the owner can set that to.
pub const SHORT_MOST: i64 = 2_000;
/// The settings' keys, as the Python Host keeps them.
pub const DISABLED_KEY: &str = "herald.disabled";
pub const SHORT_KEY: &str = "herald.speak_if_short_chars";
pub const BYPASS_KEY: &str = "herald.short_reply_bypass_enabled";

/// What the herald says for an agent: its name, or its session when it has none.
pub fn words(persona: &str, session: &str) -> String {
    let who = if persona.is_empty() { session } else { persona };
    format!("{} here, ready for an update.", python::strip(who))
}

/// How the owner has set the herald.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Settings {
    /// Off: every agent is heard at once.
    pub disabled: bool,
    /// A reply of this many signs or fewer is said at once.
    pub short: i64,
    /// Whether short replies are said at once at all.
    pub bypass: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            disabled: false,
            short: SHORT_USUAL,
            bypass: true,
        }
    }
}

/// A truth value as the Python Host reads one it kept.
fn truth(kept: Option<&str>, otherwise: bool) -> bool {
    match kept {
        Some(kept) => {
            let kept = python::strip(kept).to_lowercase();
            matches!(kept.as_str(), "1" | "true" | "yes" | "on")
        }
        None => otherwise,
    }
}

impl Settings {
    /// From what is kept under the three keys.
    pub fn stored(disabled: Option<&str>, short: Option<&str>, bypass: Option<&str>) -> Self {
        let short = short
            .and_then(|kept| python::strip(kept).parse::<i64>().ok())
            .unwrap_or(SHORT_USUAL);
        Self {
            disabled: truth(disabled, false),
            short: short.clamp(0, SHORT_MOST),
            bypass: truth(bypass, true),
        }
    }

    /// As it is kept: each key with its text.
    pub fn kept(&self) -> Vec<(String, String)> {
        vec![
            (DISABLED_KEY.to_string(), self.disabled.to_string()),
            (SHORT_KEY.to_string(), self.short.to_string()),
            (BYPASS_KEY.to_string(), self.bypass.to_string()),
        ]
    }

    /// As an app reads it.
    pub fn told(&self) -> Value {
        json!({
            "disabled": self.disabled,
            "speak_if_short_chars": self.short,
            "short_reply_bypass_enabled": self.bypass,
        })
    }

    /// What `GET /herald/settings` answers.
    pub fn screen(&self) -> Value {
        json!({
            "settings": self.told(),
            "defaults": { "speak_if_short_chars": SHORT_USUAL, "short_reply_bypass_enabled": true },
            "limits": { "speak_if_short_chars": [0, SHORT_MOST] },
        })
    }

    /// The settings after an app changed some of them. The error is what the app is told.
    pub fn changed(&self, asked: &Value) -> Result<Self, String> {
        let Some(asked) = asked.as_object() else {
            return Err("herald settings must be an object".to_string());
        };
        let mut after = *self;
        if let Some(given) = asked.get("disabled") {
            after.disabled = given
                .as_bool()
                .ok_or("disabled must be a boolean".to_string())?;
        }
        if let Some(given) = asked.get("speak_if_short_chars") {
            let whole = match given {
                Value::Number(number) if !number.is_f64() => number.as_i64(),
                _ => None,
            };
            let Some(whole) = whole else {
                // A whole number too large to hold is one, and is out of range.
                return Err(match given {
                    Value::Number(number) if !number.is_f64() => {
                        format!("speak_if_short_chars must be between 0 and {SHORT_MOST}")
                    }
                    _ => "speak_if_short_chars must be an integer".to_string(),
                });
            };
            if !(0..=SHORT_MOST).contains(&whole) {
                return Err(format!(
                    "speak_if_short_chars must be between 0 and {SHORT_MOST}"
                ));
            }
            after.short = whole;
        }
        if let Some(given) = asked.get("short_reply_bypass_enabled") {
            after.bypass = given
                .as_bool()
                .ok_or("short_reply_bypass_enabled must be a boolean".to_string())?;
        }
        Ok(after)
    }
}

/// Something to be played, told to the apps.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Play<C> {
    /// A clip of what an agent said. `across`: it is played whichever agent is open, and
    /// before what waits there (what was held, when its turn has come).
    Clip {
        session: String,
        clip: C,
        across: bool,
    },
    /// The clip that says the agent of this session has something.
    Herald { session: String },
}

/// A hand that is being raised: the herald's clip for it is being made.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attempt {
    pub session: String,
    token: u64,
}

/// What came of a clip that arrived.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Taken<C> {
    /// It is played, with what was held before it; or it is held, and nothing is played.
    Decided(Vec<Play<C>>),
    /// It is held and its agent raises its hand: the herald's clip is to be made, and
    /// [`Herald::made`] told how that went.
    Raise(Attempt),
}

/// Where the user is when a clip arrives.
#[derive(Debug, Clone, Copy)]
pub struct Moment<'a> {
    /// The session of the agent that is open; empty when none is.
    pub open: &'a str,
    pub now_ms: i64,
}

/// The herald's state: what is held, and for whom.
#[derive(Debug, Clone)]
pub struct Herald<C> {
    /// The agent a message just went to, and when.
    awaiting: Option<(String, i64)>,
    /// What is held, for each session, in the order it came.
    held: BTreeMap<String, Vec<C>>,
    /// The sessions whose hand is up: their herald was played.
    pending: BTreeSet<String>,
    /// The sessions whose herald is being made, each with the attempt's number.
    inflight: BTreeMap<String, u64>,
    attempts: u64,
}

impl<C> Default for Herald<C> {
    fn default() -> Self {
        Self {
            awaiting: None,
            held: BTreeMap::new(),
            pending: BTreeSet::new(),
            inflight: BTreeMap::new(),
            attempts: 0,
        }
    }
}

impl<C: Clone> Herald<C> {
    /// The sessions whose hand is up.
    pub fn pending(&self) -> Vec<&str> {
        self.pending.iter().map(String::as_str).collect()
    }

    /// What is held for a session.
    pub fn held(&self, session: &str) -> &[C] {
        self.held.get(session).map_or(&[], Vec::as_slice)
    }

    /// Everything that is held of one session is played, across agents or as it comes.
    fn flush(&mut self, session: &str, across: bool) -> Vec<Play<C>> {
        self.pending.remove(session);
        let held = self.held.remove(session).unwrap_or_default();
        held.into_iter()
            .map(|clip| Play::Clip {
                session: session.to_string(),
                clip,
                across,
            })
            .collect()
    }

    /// Nothing came of a clip that is held (no engine said it): it is not played later.
    /// A hand that is up for nothing else comes down.
    pub fn withdraw(&mut self, session: &str, is_it: impl Fn(&C) -> bool) {
        let Some(held) = self.held.get_mut(session) else {
            return;
        };
        held.retain(|clip| !is_it(clip));
        if held.is_empty() {
            self.held.remove(session);
            self.pending.remove(session);
        }
    }

    /// The user opened an agent. The wait for another agent's answer is over (the user
    /// has moved on), and what this agent held is played.
    pub fn opened(&mut self, session: &str) -> Vec<Play<C>> {
        if session.is_empty() {
            return Vec::new();
        }
        if self
            .awaiting
            .as_ref()
            .is_some_and(|(awaited, _)| awaited != session)
        {
            self.awaiting = None;
        }
        self.flush(session, true)
    }

    /// A message went to an agent: for a minute its answer is the one that is waited for,
    /// and what it held is played. No session: nobody is waited for.
    pub fn sent(&mut self, session: &str, now_ms: i64) -> Vec<Play<C>> {
        if session.is_empty() {
            self.awaiting = None;
            return Vec::new();
        }
        self.awaiting = Some((session.to_string(), now_ms));
        self.flush(session, true)
    }

    /// Whether the user is with this session's agent now.
    fn with(&self, session: &str, at: Moment) -> bool {
        match &self.awaiting {
            Some((awaited, since)) if at.now_ms - since < AWAITING_MS => awaited == session,
            _ => session == at.open,
        }
    }

    /// A clip that is played as it comes; behind what is held of its session, when
    /// something is and it is not that session's turn.
    fn direct(&mut self, session: &str, clip: C, its_turn: bool) -> Vec<Play<C>> {
        let mut plays = Vec::new();
        if self.held.contains_key(session) {
            if !its_turn {
                self.held.entry(session.to_string()).or_default().push(clip);
                return plays;
            }
            let across = self.pending.contains(session);
            plays = self.flush(session, across);
        }
        plays.push(Play::Clip {
            session: session.to_string(),
            clip,
            across: false,
        });
        plays
    }

    /// A clip of what an agent said arrived. `signs` is how long what it says is, when
    /// that is known; `known` whether the Host has an agent of this session.
    pub fn ingest(
        &mut self,
        (session, clip): (&str, C),
        (signs, known): (Option<i64>, bool),
        at: Moment,
        settings: Settings,
    ) -> Taken<C> {
        if session.is_empty() {
            return Taken::Decided(self.direct(session, clip, false));
        }
        if self.with(session, at) {
            return Taken::Decided(self.direct(session, clip, true));
        }
        let short = settings.bypass && signs.is_some_and(|signs| signs <= settings.short);
        if settings.disabled || short || !known {
            return Taken::Decided(self.direct(session, clip, false));
        }
        // Held before the herald's clip is made, so that a user who turns to the agent
        // meanwhile finds it.
        self.held.entry(session.to_string()).or_default().push(clip);
        if self.pending.contains(session) || self.inflight.contains_key(session) {
            return Taken::Decided(Vec::new());
        }
        self.attempts += 1;
        self.inflight.insert(session.to_string(), self.attempts);
        Taken::Raise(Attempt {
            session: session.to_string(),
            token: self.attempts,
        })
    }

    /// The herald's clip for a raised hand was made (`true`), or could not be. When the
    /// user has turned to the agent meanwhile, the herald is not played and what is held
    /// is; when the clip could not be made, the reply is played as if there were no
    /// herald.
    pub fn made(&mut self, attempt: &Attempt, clip_made: bool, at: Moment) -> Vec<Play<C>> {
        let session = attempt.session.as_str();
        if self.inflight.get(session) != Some(&attempt.token) {
            return Vec::new();
        }
        self.inflight.remove(session);
        if self.with(session, at) || !self.held.contains_key(session) {
            return self.flush(session, true);
        }
        if !clip_made {
            return self.flush(session, false);
        }
        self.pending.insert(session.to_string());
        vec![Play::Herald {
            session: session.to_string(),
        }]
    }

    /// The user said something. `name_of` gives a session's agent's name. A yes with the
    /// name of an agent whose hand is up lets what it holds be played; a no leaves it
    /// held. Gives the sessions that were granted and those that were declined, and what
    /// is played.
    pub fn said(
        &mut self,
        text: &str,
        name_of: &dyn Fn(&str) -> String,
    ) -> (Vec<String>, Vec<String>, Vec<Play<C>>) {
        if self.pending.is_empty() {
            return (Vec::new(), Vec::new(), Vec::new());
        }
        // An agent without a name cannot be called. Of two with one name the later is.
        let mut session_of: BTreeMap<String, String> = BTreeMap::new();
        let mut names: Vec<String> = Vec::new();
        for session in &self.pending {
            let name = name_of(session);
            if name.is_empty() {
                continue;
            }
            if session_of.insert(name.clone(), session.clone()).is_none() {
                names.push(name);
            }
        }
        let listed: Vec<&str> = names.iter().map(String::as_str).collect();
        let meant = meant(text, &listed);
        let sessions = |names: &[String]| -> Vec<String> {
            let found = names.iter().filter_map(|name| session_of.get(name));
            found.cloned().collect()
        };
        let (granted, declined) = (sessions(&meant.grants), sessions(&meant.declines));
        let mut plays = Vec::new();
        for session in &granted {
            plays.extend(self.flush(session, true));
        }
        (granted, declined, plays)
    }
}

// ---- what the user meant ----------------------------------------------------------------------

/// "Go ahead, talk."
const YES: &str = r"yes|yeah|yep|yup|sure|ok|okay|go\s*ahead|go\s*on|go\s*for\s*it|shoot|tell\s*me|talk(?:\s*to\s*me)?|what(?:'s| is)\s*up|what(?:'s| is)\s*it|let(?:'s| us)\s*hear\s*(?:it|you)?|hit\s*me|i'?m\s*listening";
/// "Not now."
const NO: &str = r"not\s*now|later|in\s*a\s*(?:minute|sec(?:ond)?|moment|bit)|hold\s*on|wait|hush|shh+|be\s*quiet|quiet|stop\s*talking";
/// What follows a name that is called: "Rachel, what's up".
const CALLED: &str = r"what(?:'s| is)?\s*(?:up|it|going\s*on)|go|talk|shoot|tell\s*me";
/// A yes or a no is a command, and the words of one are few: with more words than this
/// beside the names and the phrases, what was said is a message.
pub const OTHER_WORDS_MOST: usize = 2;

fn pattern(text: String) -> Regex {
    // Written here, not read from anywhere: one that does not compile is a mistake in this
    // file, which the tests of the vectors would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(&text).expect("a pattern of this module compiles")
}

static A_YES: LazyLock<Regex> = LazyLock::new(|| pattern(format!(r"(?:^|\s)(?:{YES})(?:\s|$)")));
static A_NO: LazyLock<Regex> = LazyLock::new(|| pattern(format!(r"(?:^|\s)(?:{NO})(?:\s|$)")));
/// Any of the phrases, with what ends it apart: a phrase ends before a blank or at the end,
/// and the blank is the next phrase's to begin at.
static A_PHRASE: LazyLock<Regex> =
    LazyLock::new(|| pattern(format!(r"(?:^|\s)(?:{YES}|{NO}|{CALLED})(\s|$)")));
static CALLED_FIRST: LazyLock<Regex> = LazyLock::new(|| pattern(format!("^(?:{CALLED})")));
static MARKS: LazyLock<Regex> = LazyLock::new(|| pattern(r#"[.,;:!?"]+"#.to_string()));

/// What the user's words mean for the agents whose hands are up.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Meant {
    /// The names that may speak.
    pub grants: Vec<String>,
    /// The names that are to wait.
    pub declines: Vec<String>,
}

/// Small letters, no marks, one blank between words. Apostrophes stay: "what's".
fn plain(text: &str) -> String {
    let small = text.to_lowercase();
    let unmarked = MARKS.replace_all(python::strip(&small), " ").into_owned();
    let mut plain = String::with_capacity(unmarked.len());
    let mut in_blank = false;
    for sign in unmarked.chars() {
        if python::blank(sign) {
            if !in_blank {
                plain.push(' ');
            }
            in_blank = true;
        } else {
            plain.push(sign);
            in_blank = false;
        }
    }
    plain
}

/// A name as a whole word.
fn named(name: &str) -> Option<Regex> {
    Regex::new(&format!(r"\b{}\b", regex::escape(&name.to_lowercase()))).ok()
}

/// How many words a plain utterance has beside the names and the phrases.
fn other_words(plain: &str, names: &[&str]) -> usize {
    let mut rest = String::with_capacity(plain.len());
    let mut at = 0;
    while at <= plain.len() {
        let Some(found) = A_PHRASE.captures_at(plain, at) else {
            break;
        };
        let (Some(whole), Some(end)) = (found.get(0), found.get(1)) else {
            break;
        };
        rest.push_str(&plain[at..whole.start()]);
        rest.push(' ');
        // The phrase ends where its end begins; a phrase of nothing cannot be.
        at = end.start().max(whole.start() + 1);
    }
    rest.push_str(plain.get(at..).unwrap_or(""));
    for name in names {
        if let Some(name) = named(name) {
            rest = name.replace_all(&rest, " ").into_owned();
        }
    }
    rest.split(python::blank)
        .filter(|word| !word.is_empty())
        .count()
}

/// What an utterance means for each of the names: a yes near the name or the name called
/// lets it speak, a no near the name (or a bare no, when it is the only name) makes it
/// wait. Strict on purpose: a yes that is missed is asked for again, and a backlog nobody
/// asked for is not played.
pub fn meant(text: &str, names: &[&str]) -> Meant {
    let mut meant = Meant::default();
    if text.is_empty() || names.is_empty() {
        return meant;
    }
    let plain = plain(text);
    if other_words(&plain, names) > OTHER_WORDS_MOST {
        return meant;
    }
    let bare_no = A_NO.is_match(&plain);
    let words: Vec<&str> = plain.split(' ').filter(|word| !word.is_empty()).collect();
    let as_said = python::strip(text).to_lowercase();
    for name in names {
        let small = name.to_lowercase();
        let Some(as_word) = named(name) else {
            continue;
        };
        if !as_word.is_match(&plain) {
            if bare_no && names.len() == 1 {
                meant.declines.push((*name).to_string());
            }
            continue;
        }
        let Some(place) = words.iter().position(|word| *word == small) else {
            continue;
        };
        // The three words before the name and after it: no wider, so that "go ahead
        // Rachel but Bella later" does not let Bella speak.
        let around = words[place.saturating_sub(3)..words.len().min(place + 4)].join(" ");
        if A_YES.is_match(&around) {
            meant.grants.push((*name).to_string());
            continue;
        }
        if A_NO.is_match(&around) {
            meant.declines.push((*name).to_string());
            continue;
        }
        let after = words[place + 1..words.len().min(place + 5)].join(" ");
        let asked = Regex::new(&format!(r"\b{}\b\s*\?", regex::escape(&small)));
        if CALLED_FIRST.is_match(&after) || asked.is_ok_and(|asked| asked.is_match(&as_said)) {
            meant.grants.push((*name).to_string());
        }
    }
    meant
}
