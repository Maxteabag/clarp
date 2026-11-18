//! The Oracle: whom it hands work to, and the voice it speaks with.
//!
//! The Oracle is the voice a user speaks with hands-free. It does nothing itself: what it is
//! asked to look into it hands to one agent, its contact. The Host keeps who that is, and
//! holds it against the agents it has every time it is read: a contact that names no agent
//! any more is let go and said to be stale, so that an app can ask its user to choose again.
//! And an Oracle call runs on one of two voice models, the owner's choice where this Host can
//! run it.
//!
//! These are the Python Host's rules (`server/lib/oracle_contact.py`,
//! `server/lib/oracle_voice_provider.py`), without a store: what is kept is given, and what
//! is to be kept is said. Held to that Host's own modules by
//! `tests/fixtures/oracle-vectors.json`.

use serde_json::{Value, json};

use crate::python;

pub mod attention;
pub mod conversation;
pub mod delegations;
pub mod earcons;
pub mod gemini;
pub mod handoffs;
pub mod phone;
pub mod relay;
pub mod routing;
pub mod threads;
pub mod tools;
pub mod usage;
pub mod voice;

/// Under what the contact is kept, and how long a name of one may be.
pub const CONTACT_KEY: &str = "oracle.contact_session";
pub const CONTACT_LONGEST: usize = 160;
/// Under what the choice of a voice is kept, and the voices there are.
pub const VOICE_KEY: &str = "oracle.voice_provider";
pub const VOICES: [&str; 2] = ["openai", "gemini"];
/// The model of the second voice, where the owner names no other.
pub const GEMINI_MODEL: &str = "gemini-3.8-live";

/// An agent of the Host, as the Oracle's contact is held against it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Agent<'a> {
    pub session: &'a str,
    pub persona: &'a str,
    pub archived: bool,
    pub janitor: bool,
    /// A helper of another agent, by its role.
    pub helper: bool,
    /// It has a parent.
    pub child: bool,
}

/// What a request says where a name is wanted, as Python reads it (`str(value or "")`):
/// nothing for what is false to it, a text as it is, anything else as it is written.
pub fn said(value: &Value) -> String {
    match value {
        value if !python::truthy(value) => String::new(),
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// The same without the blanks around it.
pub fn named(value: &Value) -> String {
    python::strip(&said(value)).to_string()
}

/// The agents the Oracle may hand work to: those that are not put away and are no janitors.
pub fn live<'a, 'b>(agents: &'b [Agent<'a>]) -> impl Iterator<Item = &'b Agent<'a>> {
    agents
        .iter()
        .filter(|agent| !agent.archived && !agent.janitor)
}

/// The one agent a name means, by its session or by what it is called, whatever the case of
/// the letters. A name two agents answer to means none: a contact names one agent.
pub fn resolve<'a, 'b>(name: &str, agents: &'b [Agent<'a>]) -> Option<&'b Agent<'a>> {
    let wanted = python::casefold(python::strip(name));
    if wanted.is_empty() {
        return None;
    }
    let is = |agent: &&Agent| {
        python::casefold(agent.session) == wanted || python::casefold(agent.persona) == wanted
    };
    let mut found = live(agents).filter(is);
    let first = found.next()?;
    found.next().is_none().then_some(first)
}

/// The one agent a name means among the contacts an app shows: an agent by its session; by
/// what it is called only an agent that is nobody's helper, since a name can have many
/// helpers and still be one contact. `Err`: `unknown agent` or `ambiguous agent`.
pub fn resolve_visible<'a, 'b>(
    name: &str,
    agents: &'b [Agent<'a>],
) -> Result<&'b Agent<'a>, &'static str> {
    let wanted = python::casefold(python::strip(name));
    if let Some(by_session) = live(agents).find(|agent| python::casefold(agent.session) == wanted) {
        return Ok(by_session);
    }
    let called =
        |agent: &&Agent| python::casefold(agent.persona) == wanted && !agent.helper && !agent.child;
    let mut found = live(agents).filter(called);
    let first = found.next().filter(|_| !wanted.is_empty());
    let first = first.ok_or("unknown agent")?;
    match found.next() {
        None => Ok(first),
        Some(_) => Err("ambiguous agent"),
    }
}

/// The Oracle's contact, as an app is told of it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Contact {
    pub session: String,
    pub persona: String,
    /// The contact that was kept and names no agent any more.
    pub stale: String,
}

impl Contact {
    pub fn told(&self) -> Value {
        json!({ "session": self.session, "persona": self.persona, "stale": self.stale })
    }

    fn of(agent: &Agent) -> Self {
        Self {
            session: agent.session.to_string(),
            persona: agent.persona.to_string(),
            stale: String::new(),
        }
    }
}

/// `GET /oracle/contact`: the contact that is kept, held against the agents there are.
/// `kept`: what is kept under [`CONTACT_KEY`]. Answers the contact, and whether what is kept
/// is to be let go: a contact that names no one agent any more is, and is said to be stale.
pub fn contact(kept: &str, agents: &[Agent]) -> (Contact, bool) {
    let stored = python::strip(kept);
    if stored.is_empty() {
        return (Contact::default(), false);
    }
    match resolve(stored, agents) {
        Some(agent) => (Contact::of(agent), false),
        None => {
            let gone = Contact {
                stale: stored.to_string(),
                ..Contact::default()
            };
            (gone, true)
        }
    }
}

/// `POST /oracle/contact`: a contact is chosen, by its session or by what it is called.
/// Answers the contact and what is kept from now on (the agent's session; nothing, for a
/// contact that is cleared). `Err`: the name is no agent's, in words; nothing is kept then,
/// since a contact the Oracle's tools cannot find must never be.
pub fn choose_contact(
    value: &Value,
    agents: &[Agent],
) -> Result<(Contact, Option<String>), String> {
    // The blanks around it are taken off before it is cut, and not again after.
    let asked = named(value);
    let wanted = python::first(&asked, CONTACT_LONGEST);
    if wanted.is_empty() {
        return Ok((Contact::default(), None));
    }
    match resolve(wanted, agents) {
        Some(agent) => Ok((Contact::of(agent), Some(agent.session.to_string()))),
        None => Err(format!(
            "{} is not a live agent; choose a contact from the roster",
            python::repr(&json!(wanted))
        )),
    }
}

/// The contact a call of the Oracle uses, always an agent's session or nothing: the Host's
/// own where it has one, else what the phone still sends along where that is an agent.
/// Also whether what is kept is to be let go (a contact gone stale).
pub fn contact_of_a_call(kept: &str, requested: &Value, agents: &[Agent]) -> (String, bool) {
    let (own, let_go) = contact(kept, agents);
    if !own.session.is_empty() {
        return (own.session, let_go);
    }
    let asked = named(requested);
    let found = resolve(python::first(&asked, CONTACT_LONGEST), agents);
    (
        found.map_or_else(String::new, |agent| agent.session.to_string()),
        let_go,
    )
}

/// Why a call in which everything goes to the contact is no call.
pub const NO_SUCH_CONTACT: &str = "Direct-to-primary mode requires a valid selected contact";
pub const NO_CONTACT_CHOSEN: &str = "Select a primary contact before using direct-to-primary mode";

/// The contact of a call in which everything goes to the contact: the agent the phone
/// names, where it names one, else the Host's own. A contact that is none is no call:
/// another is never taken in its place. Also whether what is kept is to be let go.
pub fn contact_of_a_direct_call(
    kept: &str,
    requested: &Value,
    agents: &[Agent],
) -> (Result<String, &'static str>, bool) {
    let asked = named(requested);
    if !asked.is_empty() {
        let found = resolve(&asked, agents).map(|agent| agent.session.to_string());
        return (found.ok_or(NO_SUCH_CONTACT), false);
    }
    let (own, let_go) = contact(kept, agents);
    let own = Some(own.session).filter(|session| !session.is_empty());
    (own.ok_or(NO_CONTACT_CHOSEN), let_go)
}

/// What of the owner's settings the choice of a voice reads.
#[derive(Debug, Clone, Copy, Default)]
pub struct Voices<'a> {
    /// What is kept under [`VOICE_KEY`]: the choice a phone made.
    pub kept: Option<&'a str>,
    /// `[oracle] voice_provider` of the owner's file.
    pub configured: Option<&'a str>,
    /// The Host has a key for the second voice.
    pub has_gemini_key: bool,
    /// `[oracle] gemini_model` and `gemini_voice`.
    pub model: &'a str,
    pub voice: &'a str,
    /// The voice the owner gave the persona called Oracle, and his usual one.
    pub of_oracle: Option<&'a str>,
    pub usual: &'a str,
}

fn a_voice(name: &str) -> Option<&'static str> {
    VOICES.into_iter().find(|voice| *voice == name)
}

/// The first of some texts that says anything.
fn first_said<'a>(texts: &[&'a str]) -> Option<&'a str> {
    texts.iter().copied().find(|text| !text.is_empty())
}

impl Voices<'_> {
    /// The choice, whether or not it can run here: the phone's over the owner's file.
    pub fn selected(&self) -> &'static str {
        let kept = python::strip(self.kept.unwrap_or("")).to_lowercase();
        let chosen = a_voice(&kept);
        let configured = || a_voice(&self.configured.unwrap_or("").to_lowercase());
        chosen.or_else(configured).unwrap_or(VOICES[0])
    }

    /// The voices this Host can run.
    pub fn available(&self) -> Vec<&'static str> {
        let second = self.has_gemini_key.then_some(VOICES[1]);
        [VOICES[0]].into_iter().chain(second).collect()
    }

    /// The voice a new call runs on: the choice, or the first where the choice cannot run.
    pub fn effective(&self) -> &'static str {
        let choice = self.selected();
        if self.available().contains(&choice) {
            choice
        } else {
            VOICES[0]
        }
    }

    /// `GET /oracle/voice-provider`.
    pub fn status(&self) -> Value {
        let (choice, current) = (self.selected(), self.effective());
        let model = first_said(&[self.model]).unwrap_or(GEMINI_MODEL);
        let voice = first_said(&[self.voice, self.of_oracle.unwrap_or(""), self.usual]);
        let voice = voice.unwrap_or("Kore");
        let fallback =
            (choice != current).then_some("Gemini Live needs a Gemini API key on this Host");
        json!({
            "provider": current, "selected": choice, "default": VOICES[0],
            "available": self.available(), "fallback_reason": fallback,
            "gemini": { "model": model, "voice": voice },
        })
    }
}

/// `POST /oracle/voice-provider`: a phone chooses the voice. Answers what is kept. `Err`:
/// it is none of the voices, in words.
pub fn choose_voice(value: &Value) -> Result<&'static str, String> {
    let asked = named(value).to_lowercase();
    a_voice(&asked).ok_or_else(|| format!("voice provider must be one of: {}", VOICES.join(", ")))
}
