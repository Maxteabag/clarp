//! Why an agent cannot answer now (Clarp #918, feature `agent_availability`,
//! Host contract 201). Every agent row of the snapshot and of
//! `GET /agents/{session}` carries `availability`, state `ok` while the
//! agent can answer; an `agent-roster` event of kind `availability` patches
//! it live. Just before a planned restart the Host sends one `host-updating`
//! event, which stands for every agent of that Host until the desktop has
//! reconnected and read the list again.
//!
//! The line shows only while the agent cannot answer the user now (Peter,
//! 26 Nov 2026: no banner, one short calm line in the chat's header,
//! never while it works visibly on the user's own message). `busy` counts
//! only while the running turn answers other agents (`working_audience`
//! `agents`); the user's own turn has the working indicator. Restarting,
//! a switch of account, a usage limit, no sign of life and the Host
//! updating show whatever turn runs.
//!
//! The words are short: "Answering other agents · 1 ahead of you",
//! "Restarting · ~20 s", "Switching account", "Host updating", "No sign of
//! life for 4 min". The Host's `text` shows where it is that short already
//! (restarting, a usage limit, the Host updating, a state this desktop
//! does not know); the desktop adds the time part itself ("~20 s", "until
//! 14:05"), counted on the Host's clock from this machine's offset to it.
//! One line in the chat list and the open chat's header, gone when the
//! state is `ok` or the agent's answer arrives. An older Host sends neither
//! and shows nothing new.
//!
//! Every wire name and every word the desktop shows for it lives here, so a
//! renamed field is a one-line change and the wording changes in one place.

use chrono::TimeZone;
use serde_json::Value;

use crate::json::Object;

/// The Host's feature name in `/server-info` (`capabilities.features`).
pub const FEATURE: &str = "agent_availability";
/// The field on an agent row and in an `agent-roster` event's `fields`.
pub const FIELD: &str = "availability";
/// The `agent-roster` kind that carries it live.
pub const ROSTER_KIND: &str = "availability";
/// The event the Host sends just before a planned restart.
pub const HOST_UPDATING_EVENT: &str = "host-updating";
const STATE: &str = "state";
const TEXT: &str = "text";
const SINCE: &str = "since_ms";
const BACK_AT: &str = "back_at_ms";
const QUEUE_AHEAD: &str = "queue_ahead";
const LAST_ALIVE: &str = "last_alive_ms";

/// Why the agent cannot answer, by the Host's state code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum State {
    Busy,
    Restarting,
    SwitchingAccount,
    UsageLimit,
    Unresponsive,
    /// The Host itself is restarting (the `host-updating` event).
    HostUpdating,
    /// A code this desktop does not know: its text shows, if any.
    Other(String),
}

impl State {
    /// None for `ok`: the agent can answer.
    fn from_code(code: &str) -> Option<Self> {
        Some(match code {
            "ok" => return None,
            "busy" => Self::Busy,
            "restarting" => Self::Restarting,
            "switching_account" => Self::SwitchingAccount,
            "usage_limit" => Self::UsageLimit,
            "unresponsive" => Self::Unresponsive,
            other => Self::Other(other.to_owned()),
        })
    }
}

/// One agent's `availability` as the Host sent it, `ok` aside.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Availability {
    pub state: State,
    /// The Host's line without its time part, or empty.
    pub text: String,
    /// When the state began (Host clock, epoch ms).
    pub since_ms: Option<i64>,
    /// When the Host expects the agent to answer again (Host clock).
    pub back_at_ms: Option<i64>,
    /// Messages read before a new one of the user's.
    pub queue_ahead: i64,
    /// The agent's last sign of life (Host clock).
    pub last_alive_ms: Option<i64>,
}

/// The time the line is worded at: this machine's clock and the Host
/// clock's offset to it (Host minus local, 0 when unknown).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Clock {
    pub local_now_ms: i64,
    pub host_offset_ms: i64,
}

impl Clock {
    pub fn now(host_offset_ms: i64) -> Self {
        Self { local_now_ms: chrono::Utc::now().timestamp_millis(), host_offset_ms }
    }

    fn host_now_ms(self) -> i64 {
        self.local_now_ms + self.host_offset_ms
    }
}

/// Whether a `/server-info` answer offers the feature.
pub fn offered_by(info: &Object) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)))
}

/// The row's availability: None when absent, null, `ok` or not an object.
pub fn read(object: &Object) -> Option<Availability> {
    parse(object.get(FIELD)?)
}

fn time(object: &Object, key: &str) -> Option<i64> {
    object.get(key).and_then(Value::as_i64).filter(|t| *t > 0)
}

fn text(object: &Object) -> String {
    object.get(TEXT).and_then(Value::as_str).unwrap_or_default().split_whitespace().collect::<Vec<_>>().join(" ")
}

/// One `availability` value: None for null, `ok` or anything but an
/// object. A state this desktop does not know is [`State::Other`].
pub fn parse(value: &Value) -> Option<Availability> {
    let object = value.as_object()?;
    let code = object.get(STATE).and_then(Value::as_str).map(str::trim).unwrap_or_default();
    Some(Availability {
        state: State::from_code(code)?,
        text: text(object),
        since_ms: time(object, SINCE),
        back_at_ms: time(object, BACK_AT),
        queue_ahead: object.get(QUEUE_AHEAD).and_then(Value::as_i64).unwrap_or(0).max(0),
        last_alive_ms: time(object, LAST_ALIVE),
    })
}

/// The `host-updating` event as the line for every agent of that Host.
pub fn host_updating(event: &Object) -> Availability {
    Availability {
        state: State::HostUpdating,
        text: text(event),
        since_ms: time(event, SINCE),
        back_at_ms: time(event, BACK_AT),
        queue_ahead: 0,
        last_alive_ms: None,
    }
}

/// Whether a `/log` delta holds the agent's answer: one of its turns from
/// the agent, written at or after `since` where both times are known (both
/// are the Host's clock). A delta holds only rows newer than the chat's, so
/// its answer ends the line before the Host's `ok` arrives; a reply from
/// before the agent stopped answering (a chat catching up) does not.
pub fn answered(log: &Object, since: Option<i64>) -> bool {
    let at = |turn: &Value| {
        let stamp = turn.get("timestamp").and_then(Value::as_str)?;
        chrono::DateTime::parse_from_rfc3339(stamp.trim()).ok().map(|t| t.timestamp_millis())
    };
    log.get("turns").and_then(Value::as_array).is_some_and(|turns| {
        turns.iter().any(|turn| {
            turn.get("role").and_then(Value::as_str) == Some("assistant")
                && match (since, at(turn)) {
                    (Some(since), Some(at)) => at >= since,
                    _ => true,
                }
        })
    })
}

/// Whether the state keeps the agent from answering the user now, given
/// who its running turn is for (`working_audience`: `user`, `agents`, or
/// empty while idle). Busy on the user's own turn, or on no turn the Host
/// names, is the working indicator's to show; busy answering other agents
/// keeps the user waiting. Every other state does, whatever turn runs.
pub fn blocks_user(availability: &Availability, working_audience: &str) -> bool {
    availability.state != State::Busy || working_audience == "agents"
}

/// The desktop's own words for a state the Host sent no text for; None for
/// a state it does not know.
fn own_words(availability: &Availability) -> Option<String> {
    Some(match availability.state {
        State::Busy => busy(availability.queue_ahead),
        State::Restarting => "Restarting".to_owned(),
        State::SwitchingAccount => SWITCHING_ACCOUNT.to_owned(),
        State::UsageLimit => "Usage limit reached".to_owned(),
        State::Unresponsive => NO_SIGN_OF_LIFE.to_owned(),
        State::HostUpdating => "Host updating".to_owned(),
        State::Other(_) => return None,
    })
}

const SWITCHING_ACCOUNT: &str = "Switching account";
const NO_SIGN_OF_LIFE: &str = "No sign of life";

/// A busy agent shows only while it answers other agents (`blocks_user`):
/// "Answering other agents · 3 ahead of you".
fn busy(queue_ahead: i64) -> String {
    match queue_ahead {
        0 => "Answering other agents".to_owned(),
        ahead => format!("Answering other agents · {ahead} ahead of you"),
    }
}

/// "~20 s" under 90 seconds, "~12 min" under an hour, "until 14:05"
/// (local 24-hour time) after; None once the time is past.
fn back(back_at: Option<i64>, clock: Clock) -> Option<String> {
    let back_at = back_at?;
    let left = back_at - clock.host_now_ms();
    if left <= 0 {
        return None;
    }
    let seconds = (left + 999) / 1000;
    if seconds < 90 {
        return Some(format!("~{seconds} s"));
    }
    if seconds < 3600 {
        return Some(format!("~{} min", (seconds + 30) / 60));
    }
    let local = chrono::Local.timestamp_millis_opt(back_at - clock.host_offset_ms).single()?;
    Some(format!("until {}", local.format("%H:%M")))
}

/// "No sign of life for 4 min": whole minutes since the last sign, at
/// least one (the Host says so only after three).
fn silent_for(alive: i64, clock: Clock) -> String {
    let minutes = ((clock.host_now_ms() - alive).max(0) / 60_000).max(1);
    format!("{NO_SIGN_OF_LIFE} for {minutes} min")
}

/// The one line for the chat list and the open chat's header, for a state
/// that blocks the user (`blocks_user`): the short words, then the time
/// part. None when there is nothing to say: a state this desktop does not
/// know, sent without text.
pub fn line(availability: &Availability, clock: Clock) -> Option<String> {
    let mut line = match availability.state {
        // The Host's words for these say less than the user needs ("Busy"
        // names no one) or more ("(usage limit)"): the desktop's own.
        State::Busy | State::SwitchingAccount => own_words(availability)?,
        State::Unresponsive => match availability.last_alive_ms {
            Some(alive) => silent_for(alive, clock),
            None => NO_SIGN_OF_LIFE.to_owned(),
        },
        _ if !availability.text.is_empty() => availability.text.clone(),
        _ => own_words(availability)?,
    };
    if let Some(back) = back(availability.back_at_ms, clock) {
        line.push_str(" · ");
        line.push_str(&back);
    }
    Some(line)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const NOW: i64 = 1_795_670_000_000;

    fn of(value: Value) -> Option<Availability> {
        parse(&value)
    }

    fn at(local_now_ms: i64) -> Clock {
        Clock { local_now_ms, host_offset_ms: 0 }
    }

    fn words(value: Value) -> Option<String> {
        line(&of(value).expect("an availability"), at(NOW))
    }

    fn says(value: Value) -> String {
        words(value).expect("a line")
    }

    fn local(ms: i64) -> String {
        chrono::Local.timestamp_millis_opt(ms).unwrap().format("%H:%M").to_string()
    }

    #[test]
    fn null_ok_or_no_object_means_the_agent_can_answer() {
        assert_eq!(of(Value::Null), None);
        assert_eq!(of(json!({"state": "ok", "text": "", "queue_ahead": 0})), None);
        assert_eq!(of(json!("busy")), None);
        assert_eq!(read(json!({"session": "sam"}).as_object().unwrap()), None);
        assert_eq!(read(json!({"session": "sam", "availability": null}).as_object().unwrap()), None);
    }

    #[test]
    fn every_field_is_read() {
        let parsed = of(json!({"state": "busy", "text": " Busy ·\n25 messages ahead of you ", "since_ms": NOW - 5_000, "back_at_ms": null,
            "queue_ahead": 25, "last_alive_ms": NOW - 1_000}));
        assert_eq!(
            parsed,
            Some(Availability {
                state: State::Busy,
                text: "Busy · 25 messages ahead of you".into(),
                since_ms: Some(NOW - 5_000),
                back_at_ms: None,
                queue_ahead: 25,
                last_alive_ms: Some(NOW - 1_000),
            })
        );
        let sparse = of(json!({"state": "restarting", "queue_ahead": -3, "since_ms": 0})).unwrap();
        assert_eq!((sparse.text.as_str(), sparse.since_ms, sparse.queue_ahead), ("", None, 0));
    }

    #[test]
    fn the_hosts_short_text_shows_as_sent_with_the_time_part_added() {
        assert_eq!(says(json!({"state": "restarting", "text": "Restarting", "back_at_ms": NOW + 20_000})), "Restarting · ~20 s");
        assert_eq!(says(json!({"state": "usage_limit", "text": "Usage limit"})), "Usage limit");
        assert_eq!(says(json!({"state": "restarting", "text": "Restarting after the Host restarted"})), "Restarting after the Host restarted");
    }

    #[test]
    fn the_desktop_says_less_where_the_host_says_more() {
        // Busy shows only for other agents' turns: it names them, and how
        // many messages go before the user's.
        assert_eq!(says(json!({"state": "busy", "text": "Busy · 25 messages ahead of you", "queue_ahead": 25})), "Answering other agents · 25 ahead of you");
        assert_eq!(says(json!({"state": "busy", "text": "Busy · 1 message ahead of you", "queue_ahead": 1})), "Answering other agents · 1 ahead of you");
        assert_eq!(says(json!({"state": "busy", "text": "Busy", "queue_ahead": 0})), "Answering other agents");
        assert_eq!(says(json!({"state": "switching_account", "text": "Switching account (usage limit)"})), "Switching account");
        assert_eq!(says(json!({"state": "switching_account", "text": "Switching account (sign-in)"})), "Switching account");
        assert_eq!(
            says(json!({"state": "unresponsive", "text": "Not responding", "last_alive_ms": NOW - 4 * 60_000 - 10_000})),
            "No sign of life for 4 min"
        );
        assert_eq!(says(json!({"state": "unresponsive", "text": "Not responding", "last_alive_ms": NOW - 40_000})), "No sign of life for 1 min", "never zero minutes");
        assert_eq!(says(json!({"state": "unresponsive", "text": "Not responding"})), "No sign of life");
    }

    #[test]
    fn empty_text_gets_the_desktops_own_words() {
        assert_eq!(says(json!({"state": "restarting", "text": "", "back_at_ms": NOW + 20_000})), "Restarting · ~20 s");
        assert_eq!(says(json!({"state": "usage_limit"})), "Usage limit reached");
        assert_eq!(says(json!({"state": "unresponsive"})), "No sign of life");
        assert_eq!(says(json!({"state": "busy", "text": "  ", "queue_ahead": 2})), "Answering other agents · 2 ahead of you", "blank text is no text");
    }

    /// Peter, 26 Nov 2026: the line shows only while the agent cannot
    /// answer the user now, never while it works visibly on the user's own
    /// message; work for other agents still keeps the user waiting.
    #[test]
    fn busy_blocks_the_user_only_on_other_agents_turns() {
        let busy = of(json!({"state": "busy", "queue_ahead": 1})).unwrap();
        assert!(blocks_user(&busy, "agents"));
        assert!(!blocks_user(&busy, "user"), "the user's own turn: the working indicator says it");
        assert!(!blocks_user(&busy, ""), "no turn the Host names: no evidence it blocks");
        for state in ["restarting", "switching_account", "usage_limit", "unresponsive", "napping"] {
            let other = of(json!({"state": state})).unwrap();
            for audience in ["user", "agents", ""] {
                assert!(blocks_user(&other, audience), "{state} shows on a turn for {audience:?}");
            }
        }
        let updating = host_updating(json!({"type": "host-updating"}).as_object().unwrap());
        assert!(blocks_user(&updating, "user"));
    }

    #[test]
    fn unknown_states_show_their_text_or_nothing() {
        assert_eq!(of(json!({"state": "napping"})).unwrap().state, State::Other("napping".into()));
        assert_eq!(words(json!({"state": "napping"})), None);
        assert_eq!(words(json!({})), None);
        assert_eq!(says(json!({"state": "napping", "text": "Taking a break", "back_at_ms": NOW + 5_000})), "Taking a break · ~5 s");
    }

    #[test]
    fn the_time_part_is_seconds_then_minutes_then_a_clock_time() {
        let back = |ms: i64| says(json!({"state": "restarting", "text": "Restarting", "back_at_ms": NOW + ms}));
        assert_eq!(back(19_200), "Restarting · ~20 s", "seconds round up");
        assert_eq!(back(400), "Restarting · ~1 s");
        assert_eq!(back(89_000), "Restarting · ~89 s");
        assert_eq!(back(90_000), "Restarting · ~2 min");
        assert_eq!(back(150_000), "Restarting · ~3 min", "minutes round to the nearest");
        assert_eq!(back(3_599_000), "Restarting · ~60 min");
        assert_eq!(back(3_600_000), format!("Restarting · until {}", local(NOW + 3_600_000)));
        assert_eq!(back(0), "Restarting", "a time that is past is left out");
        assert_eq!(back(-5_000), "Restarting");
        let limit = says(json!({"state": "usage_limit", "text": "Usage limit", "back_at_ms": NOW + 3 * 3_600_000}));
        assert_eq!(limit, format!("Usage limit · until {}", local(NOW + 3 * 3_600_000)));
        let alive = |ms: i64| says(json!({"state": "unresponsive", "last_alive_ms": NOW - ms}));
        assert_eq!(alive(239_000), "No sign of life for 3 min");
        assert_eq!(alive(-3_000), "No sign of life for 1 min", "a clock ahead never counts below one");
    }

    #[test]
    fn times_count_on_the_hosts_clock() {
        // The Host's clock is 30 s ahead of this machine's: back_at_ms 50 s
        // ahead of the Host's now is 50 s away, not 80.
        let restarting = of(json!({"state": "restarting", "back_at_ms": NOW + 30_000 + 50_000})).unwrap();
        assert_eq!(line(&restarting, Clock { local_now_ms: NOW, host_offset_ms: 30_000 }).unwrap(), "Restarting · ~50 s");
        assert_eq!(line(&restarting, at(NOW)).unwrap(), "Restarting · ~80 s", "without an offset the local clock counts");
        // A clock time is this machine's: two minutes earlier than the Host's.
        let later = of(json!({"state": "usage_limit", "back_at_ms": NOW + 7_200_000})).unwrap();
        let skewed = Clock { local_now_ms: NOW, host_offset_ms: 120_000 };
        assert_eq!(line(&later, skewed).unwrap(), format!("Usage limit reached · until {}", local(NOW + 7_200_000 - 120_000)));
        let silent = of(json!({"state": "unresponsive", "last_alive_ms": NOW + 30_000 - 5 * 60_000})).unwrap();
        assert_eq!(line(&silent, Clock { local_now_ms: NOW, host_offset_ms: 30_000 }).unwrap(), "No sign of life for 5 min");
    }

    #[test]
    fn a_host_update_is_one_line_for_every_agent() {
        let event = json!({"type": "host-updating", "text": "Host updating", "since_ms": NOW, "back_at_ms": NOW + 40_000});
        let updating = host_updating(event.as_object().unwrap());
        assert_eq!(updating.state, State::HostUpdating);
        assert_eq!(line(&updating, at(NOW)).unwrap(), "Host updating · ~40 s");
        assert_eq!(line(&updating, at(NOW + 41_000)).unwrap(), "Host updating");
        let bare = host_updating(json!({"type": "host-updating"}).as_object().unwrap());
        assert_eq!(line(&bare, at(NOW)).unwrap(), "Host updating", "no text: the desktop's words");
    }

    #[test]
    fn an_answer_in_the_log_ends_the_line() {
        let log = |turns: Value| json!({"turns": turns}).as_object().cloned().unwrap();
        assert!(answered(&log(json!([{"role": "user"}, {"role": "assistant"}])), None));
        assert!(!answered(&log(json!([{"role": "user"}])), None));
        assert!(!answered(&log(json!([])), None));
        assert!(!answered(&json!({}).as_object().cloned().unwrap(), None));
        let at = |stamp: &str| log(json!([{"role": "assistant", "timestamp": stamp}]));
        let since = chrono::DateTime::parse_from_rfc3339("2026-11-26T10:00:00Z").unwrap().timestamp_millis();
        assert!(answered(&at("2026-11-26T10:00:05Z"), Some(since)), "a reply after it stopped answering");
        assert!(!answered(&at("2026-11-26T09:59:00Z"), Some(since)), "a reply from before is not the answer");
        assert!(answered(&at(""), Some(since)), "a reply of unknown time counts");
    }

    #[test]
    fn the_feature_gate_reads_server_info() {
        let with = json!({"capabilities": {"features": ["stalled_turn", FEATURE]}});
        let without = json!({"capabilities": {"features": ["stalled_turn"]}});
        assert!(offered_by(with.as_object().unwrap()));
        assert!(!offered_by(without.as_object().unwrap()));
        assert!(!offered_by(json!({}).as_object().unwrap()));
    }
}
