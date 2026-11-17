//! The heartbeat: a turn the Host gives an idle agent that asked for one.
//!
//! An agent that has the heartbeat on is sent a short prompt from time to time, so that it
//! looks at what it has standing and acts only when something needs it. The Host decides
//! when: not while the agent works, waits for its user or was stopped by its user; not
//! sooner than the owner's interval; longer after each time the agent found nothing to do;
//! and not at all once it has gone dormant, until something wakes it. An agent that found
//! nothing to do says so with one word, and neither the prompt nor that answer is shown.
//!
//! These are the Python Host's rules (`server/lib/heartbeat.py`), held to it by
//! `tests/heartbeat_vectors.rs`: what counts as such an answer, which settings are taken and
//! why not, why an agent gets no heartbeat at a moment or that it is due, and how long its
//! interval is.

use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::python;

/// The word an agent answers a heartbeat with when nothing is to be done.
pub const OK: &str = "HEARTBEAT_OK";

/// What an agent is sent.
pub const PROMPT: &str = "This is a continuity check, not a new task. Read HEARTBEAT.md if it exists \
(workspace context). Follow it strictly. Review the durable goal/task plan \
included below, current commitments, and queued team updates. If a pending \
or in-progress plan item can be advanced safely, continue it now; do not \
reply HEARTBEAT_OK merely because no new chat message arrived. Do not infer \
or repeat old tasks from prior chats. Do not restart completed or stale work. \
Audit durable background jobs and update any job whose displayed state is stale. \
Preserve genuine active work. If no current \
work can be advanced and nothing needs attention, reply HEARTBEAT_OK and \
take no action.";

/// What stands before it for an agent whose turn the Host's restart took down.
pub const RESTART_PREFIX: &str = "The Clarp server has just restarted. Your previous turn may have been \
interrupted. Re-establish the current workspace state and continue any \
unfinished active commitment without repeating completed work.\n\n";

/// And for one whose turn was interrupted or paused by a limit.
pub const INTERRUPTED_PREFIX: &str = "Your previous turn was interrupted or paused by a system limit. \
Re-establish the current workspace state and continue any unfinished \
active commitment without repeating completed work.\n\n";

pub const DEFAULT_INTERVAL_S: i64 = 30 * 60;
pub const DEFAULT_BACKOFF_CAP_S: i64 = 60 * 60;
/// How long an agent whose turn was interrupted is tried again before it is left alone.
pub const MAX_INTERRUPTED_RETRY_S: f64 = 11.0 * 60.0 * 60.0;
/// No two heartbeats of an agent are closer than this, whatever else says.
pub const MIN_WAKE_SPACING_S: f64 = 30.0;
/// More than this many starts within the window, and the agent is left alone for now.
pub const FLOOD_WINDOW_S: f64 = 60.0;
pub const FLOOD_THRESHOLD: usize = 5;
/// An answer with the word in it and no more than this beside it is no answer to show.
pub const ACK_MAX_CHARS: usize = 300;
/// How many lines of a plan an agent is shown.
const PLAN_LINES_MOST: usize = 12;

/// The owner's settings for every agent of the Host that has the heartbeat on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub interval_sec: i64,
    /// `fixed`, `linear` or `exponential`.
    pub backoff_strategy: String,
    pub backoff_cap_sec: i64,
    /// After this many heartbeats in a row with nothing to do the agent goes dormant.
    /// Nothing: it never does.
    pub dormant_after_noops: i64,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            interval_sec: DEFAULT_INTERVAL_S,
            backoff_strategy: "exponential".to_string(),
            backoff_cap_sec: DEFAULT_BACKOFF_CAP_S,
            dormant_after_noops: 5,
        }
    }
}

const STRATEGIES: [&str; 3] = ["fixed", "linear", "exponential"];

impl Settings {
    /// The settings as the apps read and write them.
    pub fn shown(&self) -> Value {
        json!({
            "heartbeat_interval_sec": self.interval_sec,
            "heartbeat_backoff_strategy": self.backoff_strategy,
            "heartbeat_backoff_cap_sec": self.backoff_cap_sec,
            "heartbeat_dormant_after_noops": self.dormant_after_noops,
        })
    }

    /// The settings after a change an app asks for: what it names is taken, what it leaves
    /// out stays. `Err`: why the change is not taken, in the Python Host's words.
    pub fn changed(&self, said: &Value) -> Result<Self, String> {
        let Some(said) = said.as_object() else {
            return Err("heartbeat settings must be an object".to_string());
        };
        let integer = |name: &str, current: i64| match said.get(name) {
            None => Ok(current),
            Some(Value::Number(number)) if number.is_i64() || number.is_u64() => {
                Ok(number.as_i64().unwrap_or(i64::MAX))
            }
            Some(_) => Err(format!("{name} must be an integer")),
        };
        let interval = integer("heartbeat_interval_sec", self.interval_sec)?;
        let cap = integer("heartbeat_backoff_cap_sec", self.backoff_cap_sec)?;
        let dormant = integer("heartbeat_dormant_after_noops", self.dormant_after_noops)?;
        let strategy = match said.get("heartbeat_backoff_strategy") {
            None => self.backoff_strategy.clone(),
            Some(Value::String(named)) => python::strip(named).to_string(),
            Some(_) => String::new(),
        };
        if !STRATEGIES.contains(&strategy.as_str()) {
            return Err("invalid heartbeat_backoff_strategy".to_string());
        }
        if !(60..=86_400).contains(&interval) {
            return Err("heartbeat_interval_sec must be 60...86400".to_string());
        }
        if !(interval..=86_400).contains(&cap) {
            return Err("heartbeat_backoff_cap_sec must be interval...86400".to_string());
        }
        if !(0..=100).contains(&dormant) {
            return Err("heartbeat_dormant_after_noops must be 0...100".to_string());
        }
        Ok(Self {
            interval_sec: interval,
            backoff_strategy: strategy,
            backoff_cap_sec: cap,
            dormant_after_noops: dormant,
        })
    }
}

// ---- the prompt, and the answer that says nothing is to be done ------------------------------

/// Whether a message is the heartbeat's prompt: it is not shown in the conversation.
pub fn is_prompt(text: &str) -> bool {
    let clean = python::strip(text);
    clean.starts_with(PROMPT)
        || clean.starts_with(RESTART_PREFIX)
        || clean.starts_with(INTERRUPTED_PREFIX)
}

/// The word at the beginning of an answer with what follows it that is no word, or at its
/// end with what stands around it that is no word.
static WORD_AT_AN_EDGE: LazyLock<Regex> = LazyLock::new(|| {
    // Written here, not read from anywhere: one that does not compile is a mistake in this
    // file, which the tests of this module would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(r"(?:^HEARTBEAT_OK\W*|\W*HEARTBEAT_OK\W*$)")
        .expect("a pattern of this module compiles")
});

/// A text with every run of blanks as one space.
fn spaced(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_blanks = false;
    for sign in text.chars() {
        if python::blank(sign) {
            in_blanks = true;
        } else {
            if in_blanks {
                out.push(' ');
            }
            in_blanks = false;
            out.push(sign);
        }
    }
    if in_blanks {
        out.push(' ');
    }
    out
}

/// What an answer to a heartbeat is: whether it says no more than that nothing is to be
/// done, and so is not shown, and the answer as it is shown otherwise.
///
/// The word at the beginning or at the end is taken off. When what is left is nothing, or
/// short enough to be a remark beside it, the whole answer is not shown. The word in the
/// middle of an answer is left where it is.
pub fn answered(text: &str) -> (bool, String) {
    if !text.contains(OK) {
        return (false, text.to_string());
    }
    let stripped = python::strip(text);
    if !stripped.starts_with(OK) && !stripped.ends_with(OK) {
        return (false, text.to_string());
    }
    let remaining = WORD_AT_AN_EDGE.replace_all(stripped, "");
    let remaining = spaced(python::strip(&remaining));
    if remaining.chars().count() <= ACK_MAX_CHARS {
        (true, String::new())
    } else {
        (false, remaining)
    }
}

/// What the conversation keeps in place of an answer that says no more than that nothing is
/// to be done.
pub const NOTHING_TO_DO: &str = "Heartbeat check: no action needed.";

/// Whether an answer is the word and nothing else: no letter or digit beside it.
pub fn only_the_word(text: &str) -> bool {
    let beside = text.replace(OK, "");
    let of_a_word = |sign: char| sign.is_alphanumeric() || sign == '_';
    text.contains(OK) && !beside.chars().any(of_a_word)
}

/// What an answer counts as for its agent's heartbeat.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Counted {
    /// Nothing: it is no heartbeat's answer, or says nothing either way.
    Neither,
    /// The agent found nothing to do.
    Nothing,
    /// The agent did or said something.
    Activity,
}

/// What the conversation keeps of an answer, and what the answer counts as. In a
/// heartbeat's own turn the word at an edge of the answer is the heartbeat's; in any other
/// turn an answer is kept as it is, unless it is nothing but the word.
pub fn kept(text: &str, in_its_turn: bool) -> (String, Counted) {
    let (nothing_to_do, said) = if in_its_turn {
        answered(text)
    } else {
        (only_the_word(text), text.to_string())
    };
    if nothing_to_do {
        let counted = if in_its_turn {
            Counted::Nothing
        } else {
            Counted::Neither
        };
        return (NOTHING_TO_DO.to_string(), counted);
    }
    let counted = if in_its_turn && !is_neutral_status(&said) {
        Counted::Activity
    } else {
        Counted::Neither
    };
    (said, counted)
}

/// Whether what an agent says while it answers a heartbeat is no more than a word about the
/// heartbeat itself: nothing, the word, or a short line that begins as such lines do.
pub fn is_neutral_status(text: &str) -> bool {
    let clean = python::strip(text);
    let normalized = spaced(clean).to_lowercase();
    if normalized.is_empty() || normalized.contains(&OK.to_lowercase()) {
        return true;
    }
    if clean.chars().count() > ACK_MAX_CHARS {
        return false;
    }
    ["checking heartbeat", "heartbeat check", "heartbeat pass"]
        .iter()
        .any(|begin| normalized.starts_with(begin))
}

/// One thing of a plan an agent is shown with its heartbeat: how it stands, and what it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanLine {
    pub status: String,
    pub title: String,
}

/// The prompt an agent is sent: the words of the heartbeat, before them a word about a turn
/// that was interrupted, and after them the plan the agent has standing. `plan` is the
/// plan's title and everything in it in its order, each thing followed by its parts; `None`
/// when the agent has no plan that is active.
pub fn prompt_text(interrupted: bool, plan: Option<(&str, &[PlanLine])>) -> String {
    let mut text = String::new();
    if interrupted {
        text.push_str(INTERRUPTED_PREFIX);
    }
    text.push_str(PROMPT);
    let Some((title, lines)) = plan else {
        text.push_str("\n\nDurable plan: none active.");
        return text;
    };
    text.push_str("\n\nDurable plan: ");
    text.push_str(python::first(title, 240));
    let open = lines.iter().filter(|line| {
        let standing = matches!(line.status.as_str(), "pending" | "in_progress" | "blocked");
        standing && !python::strip(&line.title).is_empty()
    });
    let mut shown = 0;
    for line in open.take(PLAN_LINES_MOST) {
        let title = python::first(python::strip(&line.title), 300);
        text.push_str(&format!("\n- [{}] {title}", line.status));
        shown += 1;
    }
    if shown == 0 {
        text.push_str("\n- No pending, in-progress, or blocked items.");
    }
    text
}

// ---- when an agent is due --------------------------------------------------------------------

/// What an agent last did, as the Host recorded it: the kind of its state, when (in
/// milliseconds), and what was noted with it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Latest {
    pub kind: String,
    pub ts_ms: f64,
    pub detail: Value,
}

impl Latest {
    /// Its user stopped it: it is not woken against its user's word.
    pub fn stopped_by_its_user(&self) -> bool {
        self.detail["source"] == "user_stop" || self.detail["reason"] == "interrupted"
    }

    /// The Host's own restart took its turn down: taking it up again is the restart's, not
    /// the heartbeat's.
    pub fn taken_down_by_a_restart(&self) -> bool {
        self.detail["source"] == "server_restart"
    }

    /// Its turn was interrupted, and not by its user.
    pub fn interrupted(&self) -> bool {
        self.kind == "interrupted" && !self.stopped_by_its_user()
    }

    /// When its turn was interrupted, in seconds; nothing when it was not.
    fn interrupted_at(&self) -> f64 {
        if self.kind == "interrupted" {
            self.ts_ms / 1000.0
        } else {
            0.0
        }
    }
}

/// What the decision needs to know of an agent at a moment.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Known {
    pub latest: Latest,
    /// A turn of it runs or waits its turn.
    pub busy: bool,
    /// Its tool still holds something that lives.
    pub active: bool,
    pub compacting: bool,
    /// Why it is left alone because somebody spoke with it a moment ago; empty when nobody
    /// did.
    pub recent_activity: String,
}

/// What the heartbeat remembers of an agent.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct State {
    /// When its last heartbeat was started, in seconds. Nothing: never.
    pub last_started: f64,
    /// When its last heartbeats were started, the newest last.
    pub recent_starts: Vec<f64>,
    /// How many heartbeats in a row found nothing to do.
    pub noop_streak: i64,
    pub dormant: bool,
    /// The newest sign from outside that was taken as a reason to wake it, in milliseconds.
    pub last_wake_signal_ms: i64,
    /// When the run of interruptions the agent is in began, in seconds: the first turn of
    /// it that was cut off or failed, with no turn of it completed since. Nothing: it is in
    /// none. An agent is tried again for so long from there, and not from the last heartbeat
    /// that failed in its turn (the Python Host counts from the last: issue 396 there).
    pub interrupted_since: f64,
}

impl State {
    /// A heartbeat found nothing to do: the next one comes later, and after enough of them
    /// the agent goes dormant, unless its turn was interrupted and is still to be taken up.
    pub fn found_nothing(&mut self, policy: &Settings, interrupted: bool) {
        self.noop_streak += 1;
        let enough =
            policy.dormant_after_noops > 0 && self.noop_streak >= policy.dormant_after_noops;
        if !interrupted && enough {
            self.dormant = true;
        }
    }

    /// The agent did something: it is asked again at the owner's interval.
    pub fn was_active(&mut self) {
        self.noop_streak = 0;
        self.dormant = false;
        self.interrupted_since = 0.0;
    }

    /// What the agent last did is looked at. A turn of it that was cut off or failed, and
    /// not by its user, begins a run of interruptions where it is in none; a turn of it
    /// that completed, and its user's own stop, end the run. While a turn of it runs
    /// nothing is known yet.
    pub fn saw(&mut self, latest: &Latest) {
        if latest.interrupted() {
            if self.interrupted_since == 0.0 {
                self.interrupted_since = latest.interrupted_at();
            }
        } else if matches!(
            latest.kind.as_str(),
            "done" | "idle" | "background" | "interrupted"
        ) {
            self.interrupted_since = 0.0;
        }
    }

    /// Whether the agent has been interrupted for as long as it is tried again: counted
    /// from the turn that was interrupted last, and from where its run of interruptions
    /// began, whichever is longer ago.
    fn interrupted_long_ago(&self, latest: &Latest, now: f64) -> bool {
        let at = latest.interrupted_at();
        let since = match self.interrupted_since {
            run if run != 0.0 && at != 0.0 => run.min(at),
            _ => at,
        };
        since != 0.0 && now - since >= MAX_INTERRUPTED_RETRY_S
    }

    /// A heartbeat is started now.
    pub fn started(&mut self, now: f64, latest_signal_ms: i64) {
        self.last_started = now;
        self.last_wake_signal_ms = self.last_wake_signal_ms.max(latest_signal_ms);
        self.recent_starts.push(now);
        let most = FLOOD_THRESHOLD + 1;
        if self.recent_starts.len() > most {
            let older = self.recent_starts.len() - most;
            self.recent_starts.drain(..older);
        }
    }

    /// A sign from outside (its user wrote, its team moved): an agent that had backed off
    /// or gone dormant is asked again at the owner's interval. Answers whether the sign was
    /// new.
    pub fn woken_by(&mut self, latest_signal_ms: i64) -> bool {
        if latest_signal_ms <= 0 || latest_signal_ms <= self.last_wake_signal_ms {
            return false;
        }
        self.last_wake_signal_ms = latest_signal_ms;
        self.was_active();
        true
    }

    fn flooded(&self, now: f64) -> bool {
        let since = now - FLOOD_WINDOW_S;
        let starts = self.recent_starts.iter();
        starts.filter(|started| **started >= since).count() >= FLOOD_THRESHOLD
    }
}

/// How long an agent's interval is now, in seconds: the owner's, longer after each
/// heartbeat that found nothing to do, by the owner's strategy and up to the owner's cap.
/// An agent whose turn was interrupted is asked at the plain interval.
pub fn interval(state: &State, policy: &Settings, interrupted: bool) -> i64 {
    let (base, cap) = (policy.interval_sec, policy.backoff_cap_sec);
    if interrupted {
        return base.min(cap);
    }
    if state.noop_streak <= 0 {
        return base;
    }
    let longer = match policy.backoff_strategy.as_str() {
        "fixed" => base,
        "linear" => base.saturating_mul(state.noop_streak.saturating_add(1)),
        _ => match u32::try_from(state.noop_streak) {
            Ok(doublings) if doublings < 62 => base.saturating_mul(1_i64 << doublings),
            _ => i64::MAX,
        },
    };
    cap.min(longer)
}

/// Why an agent gets no heartbeat now; empty when one is due.
///
/// `dormant` for an agent whose turn was interrupted long ago is the caller's to remember.
pub fn skip_reason(known: &Known, state: &State, policy: &Settings, now: f64) -> String {
    let latest = &known.latest;
    let interrupted_kind = latest.kind == "interrupted";
    if interrupted_kind && latest.taken_down_by_a_restart() {
        return "restart-owner-handoff".to_string();
    }
    if latest.kind == "waiting" || (interrupted_kind && latest.stopped_by_its_user()) {
        return latest.kind.clone();
    }
    for (holds, why) in [
        (known.busy, "busy"),
        (known.active, "active"),
        (known.compacting, "compacting"),
    ] {
        if holds {
            return why.to_string();
        }
    }
    if !known.recent_activity.is_empty() {
        return known.recent_activity.clone();
    }
    let interrupted_at = latest.interrupted_at();
    let interrupted = latest.interrupted();
    if interrupted && state.interrupted_long_ago(latest, now) {
        return "dormant".to_string();
    }
    let last_event = state.last_started.max(interrupted_at);
    if last_event != 0.0 && now - last_event < MIN_WAKE_SPACING_S {
        return "min-spacing".to_string();
    }
    let due_after = interval(state, policy, interrupted) as f64;
    if last_event != 0.0 && now - last_event < due_after {
        return "not-due".to_string();
    }
    if state.flooded(now) {
        return "flood".to_string();
    }
    if state.dormant {
        return "dormant".to_string();
    }
    String::new()
}

/// What an agent's profile shows of its heartbeat: whether it is on, when the last was
/// started and when the next is due, how long its interval is now, and whether it has gone
/// dormant. `quiet_until` is the moment before which the agent is left alone because
/// somebody spoke with it, in seconds; nothing when nobody did. `keeper` says that a
/// keeper serves the agent instead of the fixed heartbeat.
pub fn schedule(
    enabled: bool,
    (latest, state): (&Latest, &State),
    policy: &Settings,
    (now, quiet_until, keeper): (f64, f64, bool),
) -> Value {
    let interrupted = latest.interrupted();
    let interrupted_at = latest.interrupted_at();
    let long_ago = state.interrupted_long_ago(latest, now);
    let dormant = state.dormant || (interrupted && long_ago);
    let mut shown = state.clone();
    shown.dormant = dormant;
    let every = interval(&shown, policy, interrupted);
    let held =
        latest.kind == "waiting" || (latest.kind == "interrupted" && latest.stopped_by_its_user());
    let next_at = (enabled && !dormant && !keeper && !held).then(|| {
        let last_event = state.last_started.max(interrupted_at);
        let due = if last_event != 0.0 {
            last_event + every as f64
        } else {
            now
        };
        now.max(due.max(quiet_until))
    });
    let ms = |seconds: f64| (seconds * 1000.0) as i64;
    json!({
        "enabled": enabled,
        "last_started_at": (state.last_started != 0.0).then(|| ms(state.last_started)),
        "next_heartbeat_at": next_at.map(ms),
        "effective_interval_sec": every,
        "noop_streak": state.noop_streak,
        "dormant": dormant,
    })
}

/// Whether a moment of the day is outside the hours the owner wants heartbeats in
/// (`HH:MM-HH:MM`; the end may be `24:00`). Nothing said, or something that is no such
/// span: no hour is outside.
pub fn outside_active_hours(span: &str, minute_of_day: u32) -> bool {
    let span = python::strip(span);
    let Some((start, end)) = span.split_once('-') else {
        return false;
    };
    let clock = |text: &str, to_midnight: bool| -> Option<u32> {
        let signs = text.as_bytes();
        let shaped = signs.len() == 5
            && signs[2] == b':'
            && [0, 1, 3, 4].iter().all(|at| signs[*at].is_ascii_digit());
        if !shaped {
            return None;
        }
        let (hour, minute): (u32, u32) = (text[..2].parse().ok()?, text[3..].parse().ok()?);
        match (hour, minute) {
            (_, 60..) => None,
            (24, 0) if to_midnight => Some(24 * 60),
            (0..=23, _) => Some(hour * 60 + minute),
            _ => None,
        }
    };
    let (Some(start), Some(end)) = (clock(start, false), clock(end, true)) else {
        return false;
    };
    if start == end {
        return true;
    }
    let inside = if end > start {
        (start..end).contains(&minute_of_day)
    } else {
        minute_of_day >= start || minute_of_day < end
    };
    !inside
}
