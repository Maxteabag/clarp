//! The Quota keeper (the janitor job `quota-monitor`): what it reads of the tools' accounts,
//! when it tells the owner, and what it keeps of each window between two looks.
//!
//! This is the Python Host's rule (`server/lib/janitor_autonomy.py`: `quota_window_key`,
//! `seed_quota_window_state`, `quota_crossing`, the loop of `AutonomyJanitors.quota_once`
//! over a reading's windows and the receipt it makes of each crossing; Claude's
//! `quota_identity` in `server/lib/backend/claude.py`), as a thing that is handed a reading,
//! the keeper's options and its memory, and answers what to keep and whom to tell. Held to
//! that code's own answers (`tests/fixtures/quota-keeper-vectors.json`, written by
//! `server-rs/tools/quota-keeper-vectors.py`).
//!
//! **A window** of a reading counts where it is fresh, says how much of it is used as a
//! number, and says when that was seen. It is remembered by its tool, its account and its
//! identity: its id, or for Claude, which says the moment a window begins anew a little
//! differently from one reading to the next, its kind and that moment to the minute. What is
//! remembered is when it was last seen and what was left of it then.
//!
//! **The owner is told** once where what is left of a window falls to the keeper's threshold
//! or under it from above, and once more where it falls to nothing. A reading that is not
//! newer than the one remembered says nothing and changes nothing; one that says more than
//! all or less than nothing was used is no reading.
//!
//! A window whose time of seeing is no text (nothing, a number) is passed over, and the rest
//! are read (R100, fixed by the Python Host's PR 456). A notice nobody got because the
//! transport failed waits and is tried again (R105, its PR 457): [`delivering`], [`after_push`].

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::adaptive::{self, State};
use crate::python;

/// The job, as the catalogue names it.
pub const ROLE: &str = "quota-monitor";
/// What the name of a window's memory begins with.
pub const WINDOW: &str = "quota-keeper.window.";
/// The tools whose notices an automatic Hotseat switcher tells itself.
pub const SWITCHED: [&str; 2] = ["claude", "codex"];
/// What a receipt of a crossing gives as its reason.
pub const REASON: &str = "quota_threshold";
/// What a receipt says where the switcher tells the owner itself.
pub const SWITCHER_TELLS: &str = "Automatic Hotseat owns provider notifications";
/// What a receipt not yet delivered says when its keeper was set anew.
pub const SET_ANEW: &str = "Janitor configuration changed";
/// How much of why a run of the keeper failed to recover is kept, in signs.
const REASON_MOST: usize = 500;

/// A value's digest as the Python Host takes it (`janitor_autonomy.digest`): SHA-256 of its
/// JSON with the fields in the order of their names, nothing between them, in ASCII.
pub fn digest(value: &Value) -> String {
    crate::live_call::sum(python::sorted_ascii(value).as_bytes())
}

/// Python's `float // float` (`float_floor_div`): the quotient taken down, as Python takes it.
fn floor_div(dividend: f64, divisor: f64) -> f64 {
    let rest = dividend % divisor;
    let mut quotient = (dividend - rest) / divisor;
    if rest != 0.0 && (divisor < 0.0) != (rest < 0.0) {
        quotient -= 1.0;
    }
    if quotient == 0.0 {
        return 0f64.copysign(dividend / divisor);
    }
    let floor = quotient.floor();
    if quotient - floor > 0.5 {
        floor + 1.0
    } else {
        floor
    }
}

/// A moment written as `datetime.fromisoformat` reads it once a `Z` is read as `+00:00`: the
/// microseconds since the epoch, and whether it said its offset. One without its offset is
/// taken in UTC (the Python Host takes it in the machine's zone, which its own readings,
/// written in UTC with their `Z`, never need).
pub fn instant(text: &str) -> Option<(i64, bool)> {
    let text = text.replace('Z', "+00:00");
    if let Ok(at) = text.parse::<jiff::Timestamp>() {
        return Some((at.as_microsecond(), true));
    }
    let civil = text.parse::<jiff::civil::DateTime>().ok()?;
    let at = civil.to_zoned(jiff::tz::TimeZone::UTC).ok()?;
    Some((at.timestamp().as_microsecond(), false))
}

/// Python's `timestamp()` of a moment, in seconds, a fraction.
fn seconds(microseconds: i64) -> f64 {
    microseconds as f64 / 1e6
}

/// When a window was seen, in milliseconds, as the keeper reads it:
/// `int(fromisoformat(observed_at).timestamp() * 1000)`. `None`: no moment.
pub fn stamp(observed_at: &Value) -> Option<i64> {
    let (at, _) = instant(observed_at.as_str()?)?;
    Some((seconds(at) * 1000.0).trunc() as i64)
}

/// What a window is told apart by: its id, or for Claude, whose moment a window begins anew
/// wavers by a fraction of a second, its kind and that moment to the nearest minute, where
/// both are said and the moment says its offset.
pub fn identity(provider: &str, window: &Value) -> Value {
    let id = window["window_id"].clone();
    if provider != "claude" {
        return id;
    }
    let (kind, resets) = (&window["kind"], &window["resets_at"]);
    if !python::truthy(kind) || !python::truthy(resets) {
        return id;
    }
    match resets.as_str().and_then(instant) {
        Some((at, true)) => {
            let minute = floor_div(seconds(at) + 30.0, 60.0) as i64;
            json!(["claude-reset-minute-v1", kind, minute])
        }
        _ => id,
    }
}

/// The name a window's memory is kept under.
pub fn window_key(provider: &str, account: &Value, window: &Value) -> String {
    let named = json!([provider, account, identity(provider, window)]);
    format!("{WINDOW}{}", digest(&named))
}

/// The name it was kept under before Claude's windows were told apart to the minute: by the
/// window's id.
pub fn old_key(provider: &str, account: &Value, window: &Value) -> String {
    let named = json!([provider, account, window["window_id"]]);
    format!("{WINDOW}{}", digest(&named))
}

/// What the keeper remembers of the windows, by the names they are kept under: what was last
/// seen of each, `{"stamp", "remaining"}`.
pub trait Memory {
    fn get(&self, key: &str) -> Option<Value>;
    fn set(&mut self, key: &str, kept: Value);
}

/// What was kept under a window's old name is carried to its new one, where nothing is kept
/// under the new one yet.
pub fn seed(provider: &str, account: &Value, window: &Value, memory: &mut dyn Memory) {
    let key = window_key(provider, account, window);
    let old = old_key(provider, account, window);
    if key != old
        && memory.get(&key).is_none()
        && let Some(previous) = memory.get(&old)
    {
        memory.set(&key, previous);
    }
}

/// A number of JSON as a fraction, for comparing.
fn fraction(value: &Value) -> Option<f64> {
    value.as_f64()
}

/// What is left of a window, as `100 - used` makes it: whole where `used` is whole.
fn left_of(used: &Value) -> Option<Value> {
    if let Some(whole) = used.as_i64() {
        return Some(json!(100 - i128::from(whole)));
    }
    used.as_f64().map(|used| json!(100.0 - used))
}

/// One reading of a window against what is remembered of it: the memory to keep (`None`:
/// none is kept, the reading counts for nothing), and what is left where the owner is told.
pub fn crossing(
    prior: Option<&Value>,
    used: &Value,
    threshold: f64,
    stamp: i64,
) -> (Option<Value>, Option<Value>) {
    let Some(remaining) = left_of(used) else {
        return (None, None);
    };
    let left = fraction(&remaining).unwrap_or(f64::NAN);
    if !(0.0..=100.0).contains(&left) {
        return (None, None);
    }
    let prior = prior.filter(|prior| !prior.is_null());
    let before = prior.and_then(|prior| fraction(&prior["remaining"]));
    if let Some(prior) = prior.filter(|prior| python::truthy(prior))
        && prior["stamp"].as_i64().is_some_and(|kept| stamp <= kept)
    {
        return (None, None);
    }
    let above = |than: f64| before.is_some_and(|before| before > than);
    let cross = (left <= threshold && (prior.is_none() || above(threshold)))
        || (left == 0.0 && prior.is_some() && above(0.0));
    let kept = json!({ "stamp": stamp, "remaining": remaining });
    (Some(kept), cross.then(|| json!({ "remaining": remaining })))
}

/// Python's `format(number, 'g')`: six figures, no zeros at the end, and the form with an
/// exponent where the number is under a ten-thousandth or a million or more.
pub fn general(number: f64) -> String {
    if number == 0.0 {
        return if number.is_sign_negative() { "-0" } else { "0" }.to_string();
    }
    if !number.is_finite() {
        return if number.is_nan() {
            "nan".to_string()
        } else if number > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    let written = format!("{number:.5e}");
    let (figures, exponent) = written.split_once('e').unwrap_or((&written, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let trim = |text: &str| -> String {
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            text.to_string()
        }
    };
    if (-4..6).contains(&exponent) {
        let places = usize::try_from(5 - exponent).unwrap_or(0);
        trim(&format!("{number:.places$}"))
    } else {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", trim(figures), exponent.abs())
    }
}

/// The keeper, as a look reads it: its agent and the options it runs with.
#[derive(Debug, Clone, Copy)]
pub struct Owner<'a> {
    pub agent_id: &'a str,
    pub session: &'a str,
    pub name: &'a str,
    pub generation: u64,
    pub threshold: f64,
    pub recovery_mode: &'a str,
}

/// One crossing, as a receipt keeps it.
#[derive(Debug, Clone, PartialEq)]
pub struct Notice {
    pub provider: String,
    pub receipt_id: String,
    /// What the phones are told, with the keeper's mode of recovery and generation.
    pub payload: Value,
    /// Whether nothing is left of the window.
    pub spent: bool,
}

/// The crossings of one look, in the order of the reading: each window that counts is read
/// against the memory, and the memory is kept as the reading says.
pub fn looked(usage: &Value, owner: &Owner<'_>, memory: &mut dyn Memory) -> Vec<Notice> {
    let mut notices = Vec::new();
    let Some(providers) = usage["providers"].as_object() else {
        return notices;
    };
    for (provider_id, provider) in providers {
        let Some(windows) = provider["windows"].as_array() else {
            continue;
        };
        for window in windows {
            let used = &window["used_percentage"];
            if window["freshness"] != "fresh" || !used.is_number() {
                continue;
            }
            let Some(stamp) = stamp(&window["observed_at"]) else {
                continue;
            };
            if window.get("window_id").is_none() {
                continue;
            }
            let account = match provider.get("provider_instance_id") {
                Some(account) => account.clone(),
                None => json!(provider_id),
            };
            let Some(remaining) = left_of(used) else {
                continue;
            };
            if !fraction(&remaining).is_some_and(|left| (0.0..=100.0).contains(&left)) {
                continue;
            }
            seed(provider_id, &account, window, memory);
            let key = window_key(provider_id, &account, window);
            let prior = memory.get(&key);
            let (kept, told) = crossing(prior.as_ref(), used, owner.threshold, stamp);
            if let Some(kept) = kept {
                memory.set(&key, kept);
            }
            if told.is_none() {
                continue;
            }
            let receipt_id = digest(&json!([
                provider_id,
                account,
                window["window_id"],
                stamp,
                owner.generation
            ]));
            let label = window["label"].as_str().filter(|label| !label.is_empty());
            let named = match label {
                Some(label) => format!("{provider_id} {label}"),
                None => provider_id.clone(),
            };
            let left = fraction(&remaining).unwrap_or(0.0);
            let payload = json!({
                "notification_id": receipt_id,
                "session": owner.session,
                "agent_id": owner.agent_id,
                "persona": owner.name,
                "preview": format!("{named}: {}% quota remaining", general(left)),
                "push": true,
                "reason": REASON,
                "recovery_mode": owner.recovery_mode,
                "owner_generation": owner.generation,
                // What a retry needs (R105): the window it speaks of, and when that resets.
                "window_key": key,
                "resets_at": window.get("resets_at").cloned().unwrap_or(Value::Null),
            });
            notices.push(Notice {
                provider: provider_id.clone(),
                receipt_id,
                payload,
                spent: used.as_f64().is_some_and(|used| used >= 100.0),
            });
        }
    }
    notices
}

/// How a recovery is asked for at a window with nothing left, as the Python Host's
/// `request_recovery` decides it: of no runtime for a tool with no coordinated selector, nor
/// where no selector is configured for the tool (this Host: no pool of the tool with the
/// owner's selector, which is every tool under `--accounts none` and `follow`); by a card the
/// owner answers where the keeper asks; at once where it recovers by itself. `None`: none is
/// asked (the keeper only tells, or something is left).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Asked {
    Unsupported,
    Unconfigured,
    Card,
    Recover,
}

pub fn asked(provider: &str, mode: &str, spent: bool, selector: bool) -> Option<Asked> {
    if !spent || mode == "notify" {
        return None;
    }
    if !SWITCHED.contains(&provider) {
        return Some(Asked::Unsupported);
    }
    if !selector {
        return Some(Asked::Unconfigured);
    }
    Some(if mode == "ask" {
        Asked::Card
    } else {
        Asked::Recover
    })
}

/// What a look that stopped part-way says of its run (the Python Host's `_close_unfinished`,
/// R100): it failed, and what it read before is kept. `why`: the kind of what stopped it.
pub fn stopped(why: &str) -> String {
    format!("The check stopped ({why}); what it read before is kept")
}

/// What became of a notice's push: whether nobody got it because the transport failed (R105:
/// pushes on, not held back, no phone took it, and at least one send failed). One a phone took,
/// beside failures, is delivered; one that was never meant to reach anyone is not this.
pub fn transport_failed(delivery: &Value) -> bool {
    if !delivery["enabled"].as_bool().unwrap_or(false) || python::truthy(&delivery["suppressed"]) {
        return false;
    }
    let count = |key: &str| delivery[key].as_i64().unwrap_or(0);
    count("sent") == 0 && count("failed") >= 1
}

/// Whether the window a notice speaks of has begun anew by `now_ms` (`resets_at`, read as the
/// Python Host's `_iso_to_ms` reads it: a moment without its offset is in UTC).
pub fn window_has_reset(payload: &Value, now_ms: i64) -> bool {
    let reset = payload["resets_at"]
        .as_str()
        .map(str::trim)
        .filter(|at| !at.is_empty());
    let reset = reset
        .and_then(instant)
        .map(|(at, _)| (seconds(at) * 1000.0).trunc() as i64);
    reset.is_some_and(|reset| now_ms >= reset)
}

/// What a look does with a notice that waits to be delivered (`deliver_notifications`).
#[derive(Debug, Clone, PartialEq)]
pub enum Deliver {
    /// Left as it is: another keeper's, or one tried too lately to be tried again yet.
    Wait,
    /// Settled without a push, with this for what became of it.
    Settle(Value),
    /// Pushed now.
    Push,
}

/// A notice that waits, as `deliver_notifications` reads it: its id, when it was written, and
/// its payload.
pub type Waiting<'a> = (&'a str, i64, &'a Value);

/// What a look does with each notice that waits, in the order given, which is the order they
/// were written in (the Python Host's `undelivered_quota_receipts`, read in its table's order;
/// R105): one
/// of another keeper is left; one of another generation is cancelled; of the notices of one
/// window the newest is the one that is sent, and an older one is settled as superseded by it;
/// one that was tried already is let go once its window has reset, and otherwise waits until
/// the keeper's interval has passed since its last attempt.
pub fn delivering(
    waiting: &[Waiting<'_>],
    (agent_id, generation): (&str, u64),
    interval_seconds: i64,
    now_ms: i64,
) -> Vec<(String, Deliver)> {
    // Which of a window's notices is the newest: by when it was written, then by its id.
    let mut by_age: Vec<&Waiting<'_>> = waiting.iter().collect();
    by_age.sort_by(|a, b| (a.1, a.0).cmp(&(b.1, b.0)));
    let threshold = |payload: &Value| payload["reason"] == REASON;
    let mut newest: std::collections::BTreeMap<&str, &str> = std::collections::BTreeMap::new();
    for (id, _, payload) in &by_age {
        if let Some(key) = payload["window_key"]
            .as_str()
            .filter(|key| threshold(payload) && !key.is_empty())
        {
            newest.insert(key, id);
        }
    }
    let mut done = Vec::new();
    for (id, _, payload) in waiting {
        if payload["agent_id"] != agent_id {
            continue;
        }
        if payload["owner_generation"] != generation {
            done.push((
                id.to_string(),
                Deliver::Settle(json!({ "cancelled": SET_ANEW })),
            ));
            continue;
        }
        let retry = &payload["delivery_retry"];
        let attempts = retry["attempts"].as_i64().unwrap_or(0);
        let key = payload["window_key"].as_str().filter(|key| !key.is_empty());
        if let Some(newer) = key
            .filter(|_| threshold(payload))
            .and_then(|key| newest.get(key))
            .filter(|newer| **newer != *id)
        {
            let settled = json!({ "superseded": newer, "attempts": attempts });
            done.push((id.to_string(), Deliver::Settle(settled)));
            continue;
        }
        if attempts > 0 && window_has_reset(payload, now_ms) {
            let settled = json!({ "expired": "The window has reset", "attempts": attempts });
            done.push((id.to_string(), Deliver::Settle(settled)));
            continue;
        }
        let last = retry["at"].as_i64().unwrap_or(0);
        if attempts > 0 && now_ms < last + interval_seconds * 1000 {
            done.push((id.to_string(), Deliver::Wait));
            continue;
        }
        done.push((id.to_string(), Deliver::Push));
    }
    done
}

/// What is pushed of a notice: all of its payload but its tries.
pub fn pushed_of(payload: &Value) -> Value {
    let mut pushed = payload.clone();
    if let Some(fields) = pushed.as_object_mut() {
        fields.remove("delivery_retry");
    }
    pushed
}

/// What became of a notice's push, written on it: `None` where nobody got it because the
/// transport failed (it waits, its attempt written on its payload, R105); else what it is
/// settled with, the attempts with it where it was tried before.
pub fn after_push(payload: &mut Value, delivery: Value, now_ms: i64) -> Option<Value> {
    let attempts = payload["delivery_retry"]["attempts"].as_i64().unwrap_or(0);
    if payload["reason"] == REASON && transport_failed(&delivery) {
        let count = |key: &str| delivery[key].as_i64().unwrap_or(0);
        payload["delivery_retry"] = json!({
            "attempts": attempts + 1, "at": now_ms,
            "last": { "sent": count("sent"), "failed": count("failed"), "disabled": count("disabled") },
        });
        return None;
    }
    if !delivery.is_object() {
        return Some(json!({}));
    }
    let mut delivery = delivery;
    if attempts > 0 {
        delivery["attempts"] = json!(attempts + 1);
    }
    Some(delivery)
}

/// What a recovery that is asked of nothing answers, in the Python Host's words.
pub fn refused(asked: Asked) -> Option<Value> {
    match asked {
        Asked::Unsupported => {
            let why = "No coordinated account selector for this provider";
            Some(json!({ "status": "unsupported", "reason": why }))
        }
        Asked::Unconfigured => {
            let why = "No runtime account selector configured";
            Some(json!({ "status": "unconfigured", "reason": why }))
        }
        Asked::Card | Asked::Recover => None,
    }
}

/// Whether a receipt still waits for its card: one of this keeper (`agent_id` in
/// `generation`) whose recovery awaits the owner's approval and names no card. The reference
/// makes the card before it writes the receipt, and a look that fails between asks again at
/// the next; this Host writes the receipt in the look and makes the card after it, so a card
/// that was not made (it failed, or the Host stopped between the two) is asked again at every
/// look until it is. A second card where only the write of its id failed is the reference's
/// own failure mode.
pub fn unasked(payload: &Value, (agent_id, generation): (&str, u64)) -> bool {
    let recovery = &payload["recovery"];
    payload["agent_id"] == agent_id
        && payload["owner_generation"] == generation
        && recovery["status"] == "awaiting_approval"
        && recovery.get("artifact_id").is_none_or(Value::is_null)
}

/// The card the owner is asked by, in the keeper's session: what `request_recovery` asks
/// (`artifacts.create_decision`), as the fields of a request for a decision.
pub fn card(provider: &str, owner: &Owner<'_>, receipt_id: &str) -> Value {
    let mut named = provider.chars();
    let titled: String = named
        .next()
        .map(|first| first.to_uppercase().chain(named).collect())
        .unwrap_or_default();
    json!({
        "session": owner.session,
        "title": format!("{titled} quota exhausted"),
        "question": format!("Allow the configured account selector to recover unfinished {provider} turns?"),
        "context": "The runtime will drain owned processes, verify requested models and resume the original unfinished turns. Completed work and stopped queues are preserved.",
        "reference_id": receipt_id,
        "payload": {"quota_recovery": {
            "owner_id": owner.agent_id, "generation": owner.generation, "provider": provider,
            "receipt_id": receipt_id,
        }},
    })
}

/// The keeper as it stands when a recovery would run.
#[derive(Debug, Clone, Copy)]
pub struct Standing<'a> {
    pub enabled: bool,
    pub job: &'a str,
    pub generation: u64,
    pub mode: &'a str,
}

/// Whether a recovery asked for a keeper (`owner_id` in `generation`) may run now, as the
/// runtime's `runtime_recover` checks it before it asks the pool: `Err` is its answer.
/// `card`: the decision a recovery the owner approved is run by, as it stands (none where
/// the keeper recovers by itself).
pub fn may_recover(
    standing: Option<Standing<'_>>,
    (owner_id, generation, provider): (&str, u64, &str),
    card: Option<&Value>,
    selector: bool,
) -> Result<(), &'static str> {
    let Some(now) =
        standing.filter(|now| now.enabled && now.job == ROLE && now.generation == generation)
    else {
        return Err("cancelled");
    };
    if now.mode == "ask" {
        let asked = card.map(|card| &card["payload"]["quota_recovery"]);
        let accepted = card.is_some_and(|card| card["decision"]["resolved_choice"] == "accepted");
        let same = asked.is_some_and(|asked| {
            asked["owner_id"] == owner_id
                && asked["generation"] == generation
                && asked["provider"] == provider
        });
        if !accepted || !same {
            return Err("approval_required");
        }
    } else if now.mode != "automatic" {
        return Err("not_authorized");
    }
    if !SWITCHED.contains(&provider) {
        return Err("unsupported");
    }
    if !selector {
        return Err("unconfigured");
    }
    Ok(())
}

/// The cards of a keeper's session the owner accepted for this keeper in this generation and
/// that were not acted on yet (`recover_approved`), in the order given: each card's id and the
/// tool it is for.
pub fn approved<'a>(
    cards: &'a [Value],
    (owner_id, generation): (&str, u64),
    acted: impl Fn(&str) -> bool,
) -> Vec<(&'a str, &'a str)> {
    cards
        .iter()
        .filter(|card| card["decision"]["resolved_choice"] == "accepted")
        .filter_map(|card| {
            let asked = &card["payload"]["quota_recovery"];
            let ours = asked["owner_id"] == owner_id && asked["generation"] == generation;
            let id = card["artifact_id"].as_str()?;
            (ours && !acted(id)).then_some((id, asked["provider"].as_str().unwrap_or_default()))
        })
        .collect()
}

/// Whether an approved recovery is done with: it is under way, or there was nothing to take
/// up. Any other answer is tried again at the next look.
pub fn settled(status: &str) -> bool {
    status == "recovering" || status == "nothing_to_resume"
}

/// What the pool answered a recovery: whether it owns a turn to take up, and whether it took
/// the request.
pub fn recovered(taken: Option<bool>) -> &'static str {
    match taken {
        None => "nothing_to_resume",
        Some(true) => "recovering",
        Some(false) => "waiting",
    }
}

/// What a run of the keeper says of itself: how it went, its line, and its result.
pub fn ran(told: usize, recoveries: &[String]) -> (&'static str, String, Value) {
    let summary = format!("Checked provider quota; {told} threshold notifications");
    let activity = if told > 0 || !recoveries.is_empty() {
        adaptive::WORKED
    } else {
        adaptive::IDLE
    };
    let reason = python::first(&recoveries.join("; "), REASON_MOST).to_string();
    let result = json!({ "summary": summary, "reason": reason, "item_count": told });
    (activity, summary, result)
}

/// Whether a look of the keeper is due at `now_ms`: it was never armed, the moment it was
/// armed for has come, or the janitors' book changed since (`changes`, the count of its
/// writes, against the count it was armed at): the keeper may have been turned on, off or set
/// anew, and nobody has to remember to say so.
pub fn due(armed: Option<(i64, u64)>, now_ms: i64, changes: u64) -> bool {
    armed.is_none_or(|(at, count)| now_ms >= at || changes != count)
}

/// When the keeper looks again, as its lane keeps it (`janitor_autonomy.lane_state`,
/// `lane_next_run`, `lane_start`, `lane_record`): for its agent and generation, the cadence
/// that grows while looks find nothing, and when it runs next. `last_check` is the moment of
/// the last look of any keeper (the Python Host's `quota-keeper.last-check`), which a keeper
/// set anew counts from.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Lane {
    pub agent_id: String,
    pub generation: u64,
    #[serde(flatten)]
    pub cadence: State,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_run_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_check: Option<i64>,
}

impl Lane {
    /// The lane of a keeper: as it was kept where it is this keeper's in this generation;
    /// anew, but for the moment of the last look, otherwise.
    pub fn of(kept: Option<Lane>, agent_id: &str, generation: u64) -> Lane {
        let kept = kept.unwrap_or_default();
        if kept.agent_id == agent_id && kept.generation == generation {
            return kept;
        }
        Lane {
            agent_id: agent_id.to_string(),
            generation,
            last_check: kept.last_check,
            ..Lane::default()
        }
    }

    /// When the keeper looks next.
    pub fn next_run(&self, base_seconds: i64) -> i64 {
        match self.next_run_at {
            Some(at) => at,
            None => self.last_check.unwrap_or(0) + base_seconds * 1000,
        }
    }

    /// A look begins: until it says how it went, the next waits the interval as it stands.
    pub fn start(&mut self, (base, longest): (i64, i64), now_ms: i64) {
        self.last_check = Some(now_ms);
        let interval = adaptive::current(&self.cadence, base, Some(longest));
        self.next_run_at = Some(now_ms + interval * 1000);
    }

    /// A look is over: how it went sets when the next is.
    pub fn record(&mut self, (base, longest): (i64, i64), done: (&str, &str), now_ms: i64) {
        let (activity, summary) = done;
        let bounds = (base, Some(longest));
        if let Some(interval) =
            adaptive::advance(&mut self.cadence, activity, summary, bounds, Some(now_ms))
        {
            self.next_run_at = Some(now_ms + interval * 1000);
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_receipt_waits_for_its_card_only_while_its_recovery_awaits_one_and_names_none() {
        let owner = ("k-1", 3);
        let receipt = |agent: &str, generation: u64, recovery: Value| {
            json!({ "agent_id": agent, "owner_generation": generation, "recovery": recovery })
        };
        let awaiting = json!({ "status": "awaiting_approval" });
        assert!(unasked(&receipt("k-1", 3, awaiting.clone()), owner));
        let named = json!({ "status": "awaiting_approval", "artifact_id": "decision-1" });
        assert!(!unasked(&receipt("k-1", 3, named), owner));
        let unnamed = json!({ "status": "awaiting_approval", "artifact_id": null });
        assert!(unasked(&receipt("k-1", 3, unnamed), owner));
        // Another keeper, or this one set anew since: its card is not this keeper's to ask.
        assert!(!unasked(&receipt("k-2", 3, awaiting.clone()), owner));
        assert!(!unasked(&receipt("k-1", 2, awaiting), owner));
        // A recovery that runs, or none at all, waits for no card.
        let running = json!({ "status": "recovering" });
        assert!(!unasked(&receipt("k-1", 3, running), owner));
        assert!(!unasked(&json!({ "agent_id": "k-1", "owner_generation": 3 }), owner));
    }

    #[test]
    fn a_number_is_written_as_python_writes_it_with_g() {
        for (number, written) in [
            (20.0, "20"),
            (9.5, "9.5"),
            (0.5, "0.5"),
            (100.0 - 99.99, "0.01"),
            (100.0, "100"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1_234_567.0, "1.23457e+06"),
            (25.000_000_1, "25"),
            (33.333_333_333, "33.3333"),
        ] {
            assert_eq!(general(number), written, "{number}");
        }
    }

    #[test]
    fn a_look_is_due_at_its_moment_or_when_the_janitors_changed() {
        assert!(due(None, 0, 0));
        assert!(!due(Some((1_000, 7)), 999, 7));
        assert!(due(Some((1_000, 7)), 1_000, 7));
        // Nobody keeps: nothing is due until a janitor changes, and then at once.
        assert!(!due(Some((i64::MAX, 7)), 5_000, 7));
        assert!(due(Some((i64::MAX, 7)), 5_000, 8));
    }

    #[test]
    fn a_quotient_is_taken_down_as_python_takes_it() {
        assert_eq!(floor_div(119.9, 60.0), 1.0);
        assert_eq!(floor_div(120.0, 60.0), 2.0);
        assert_eq!(floor_div(-0.5, 60.0), -1.0);
    }
}
