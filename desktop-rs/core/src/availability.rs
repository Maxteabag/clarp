//! Why an agent cannot answer now (Clarp #918, feature `agent_availability`,
//! Host contract 201). Every agent row of the snapshot and of
//! `GET /agents/{session}` carries `availability`, state `ok` while the
//! agent can answer; an `agent-roster` event of kind `availability` patches
//! it live. Just before a planned restart the Host sends one `host-updating`
//! event, which stands for every agent of that Host until the desktop has
//! reconnected and read the list again.
//!
//! The Host sends its calm line as `text`, without the time part. The chat
//! list shows it as sent and adds the time part itself ("back in ~20 s",
//! "back at 14:05", "last sign of life 4 min ago"), counted on the Host's
//! clock from this machine's offset to it. A state whose text is empty gets
//! the desktop's own words ([`line`]). Gone when the state is `ok` or the
//! agent's answer arrives. An older Host sends neither and shows nothing new.
//!
//! The open chat's header says it shorter and only while the agent cannot
//! answer the user now ([`header_line`]; Peter, 26 Nov 2026: no banner, one
//! short calm line in the subtitle, never while it works visibly on the
//! user's own message): "Answering other agents · 1 ahead of you",
//! "Restarting · ~20 s", "Switching account", "Host updating", "No sign of
//! life for 4 min".
//!
//! Every wire name and every word the desktop shows for it lives here, so a
//! renamed field is a one-line change and the wording changes in one place.

use chrono::TimeZone;
use serde_json::Value;

use crate::json::Object;
use crate::tr;

// i18n-screen: chat list

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

/// The desktop's own words for a state the Host sent no text for; None for
/// a state it does not know.
fn own_words(availability: &Availability) -> Option<String> {
    Some(match availability.state {
        State::Busy => match availability.queue_ahead {
            // i18n: chat list and chat header, the agent is busy (adjective)
            0 => tr!("Busy"),
            // i18n: chat list and chat header, the agent is busy and the user's message waits behind others; {n} is how many
            ahead => tr!("Busy · {n} message ahead of you" | "Busy · {n} messages ahead of you" % ahead),
        },
        // i18n: chat list and chat header, the agent is restarting (verb, ongoing)
        State::Restarting => tr!("Restarting"),
        // i18n: chat list and chat header, the agent moves to another account because this one hit its usage limit
        State::SwitchingAccount => tr!("Switching account (usage limit)"),
        // i18n: chat list and chat header, the agent's account hit its usage limit
        State::UsageLimit => tr!("Usage limit reached"),
        // i18n: chat list and chat header, the agent does not answer
        State::Unresponsive => tr!("Not responding"),
        // i18n: chat list and chat header, the Host is installing an update (Host is the product name)
        State::HostUpdating => tr!("Host updating"),
        State::Other(_) => return None,
    })
}

/// "back in ~20 s" under 90 seconds, "back in ~12 min" under an hour,
/// "back at 14:05" (local 24-hour time) after; None once the time is past.
fn back(back_at: Option<i64>, clock: Clock) -> Option<String> {
    let back_at = back_at?;
    let left = back_at - clock.host_now_ms();
    if left <= 0 {
        return None;
    }
    let seconds = (left + 999) / 1000;
    if seconds < 90 {
        // i18n: chat list, when the agent is expected back; {} is a number of seconds (about)
        return Some(tr!("back in ~{} s", seconds));
    }
    if seconds < 3600 {
        // i18n: chat list, when the agent is expected back; {} is a number of minutes (about)
        return Some(tr!("back in ~{} min", (seconds + 30) / 60));
    }
    let when = return_moment(back_at, clock)?;
    // i18n: chat list, when the agent is expected back; {} is the localized return time, including the day when needed
    Some(tr!("back at {}", when))
}

fn return_moment(host_ms: i64, clock: Clock) -> Option<String> {
    let at = chrono::Local.timestamp_millis_opt(host_ms - clock.host_offset_ms).single()?;
    let now = chrono::Local.timestamp_millis_opt(clock.local_now_ms).single()?;
    Some(crate::i18n::format::moment(crate::i18n::current(), at.naive_local(), now.date_naive(), crate::i18n::format::Clock::H24))
}

/// "last sign of life 4 min ago", or "just now" under a minute.
fn last_alive(alive: i64, clock: Clock) -> String {
    let minutes = (clock.host_now_ms() - alive).max(0) / 60_000;
    if minutes < 1 {
        // i18n: chat list, after Not responding: the agent's last activity was under a minute ago
        tr!("last sign of life just now")
    } else {
        // i18n: chat list, after Not responding: minutes since the agent's last activity; {} is the number
        tr!("last sign of life {} min ago", minutes)
    }
}

/// The one line for the chat list and the open chat's header: the Host's
/// text as sent (the desktop's own words when it is empty), then the time
/// part. None when there is nothing to say: a state this desktop does not
/// know, sent without text.
pub fn line(availability: &Availability, clock: Clock) -> Option<String> {
    let mut line = if availability.text.is_empty() { own_words(availability)? } else { availability.text.clone() };
    if let Some(back) = back(availability.back_at_ms, clock) {
        line.push_str(" · ");
        line.push_str(&back);
    }
    if availability.state == State::Unresponsive
        && let Some(alive) = availability.last_alive_ms
    {
        line.push_str(" · ");
        line.push_str(&last_alive(alive, clock));
    }
    Some(line)
}

/// Whether the state keeps the agent from answering the user now, given
/// who its running turn is for (`working_audience`: `user`, `agents`, or
/// empty while idle). Busy on the user's own turn, or on no turn the Host
/// names, is the working indicator's to show; busy answering other agents
/// keeps the user waiting. Every other state does, whatever turn runs.
pub fn blocks_user(availability: &Availability, working_audience: &str) -> bool {
    availability.state != State::Busy || working_audience == "agents"
}

/// The header's time part: "~20 s" under 90 seconds, "~12 min" under an
/// hour, "until 14:05" (local 24-hour time) after; None once it is past.
fn header_back(back_at: Option<i64>, clock: Clock) -> Option<String> {
    let back_at = back_at?;
    let left = back_at - clock.host_now_ms();
    if left <= 0 {
        return None;
    }
    let seconds = (left + 999) / 1000;
    if seconds < 90 {
        // i18n: chat header, how long until the agent is back; {} is a number of seconds (about)
        return Some(tr!("~{} s", seconds));
    }
    if seconds < 3600 {
        // i18n: chat header, how long until the agent is back; {} is a number of minutes (about)
        return Some(tr!("~{} min", (seconds + 30) / 60));
    }
    let when = return_moment(back_at, clock)?;
    // i18n: chat header, until when the agent is away; {} is the localized return time, including the day when needed
    Some(tr!("until {}", when))
}

/// The open chat's header subtitle: why the agent cannot answer the user
/// now, short, or None while it can (`blocks_user`) or there is nothing to
/// say (a state this desktop does not know, sent without text). The Host's
/// text shows where it is short already (restarting, a usage limit, the
/// Host updating, an unknown state); busy and a switch of account get the
/// desktop's shorter words, no sign of life counts its minutes.
pub fn header_line(availability: &Availability, working_audience: &str, clock: Clock) -> Option<String> {
    if !blocks_user(availability, working_audience) {
        return None;
    }
    let mut line = match availability.state {
        State::Busy => match availability.queue_ahead {
            // i18n: chat header, the agent is busy answering other agents
            0 => tr!("Answering other agents"),
            // i18n: chat header, the agent answers other agents first; {} is how many messages wait ahead of the user's
            ahead => tr!("Answering other agents · {} ahead of you", ahead),
        },
        // i18n: chat header, the agent moves to another account
        State::SwitchingAccount => tr!("Switching account"),
        State::Unresponsive => match availability.last_alive_ms {
            // At least a minute: the Host says so only after three.
            // i18n: chat header, the agent has shown no activity for this long; {} is a number of minutes
            Some(alive) => tr!("No sign of life for {} min", ((clock.host_now_ms() - alive).max(0) / 60_000).max(1)),
            // i18n: chat header, the agent has shown no activity
            None => tr!("No sign of life"),
        },
        _ if !availability.text.is_empty() => availability.text.clone(),
        _ => own_words(availability)?,
    };
    if let Some(back) = header_back(availability.back_at_ms, clock) {
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
    fn the_hosts_text_shows_as_sent_with_the_time_part_added() {
        assert_eq!(says(json!({"state": "restarting", "text": "Restarting", "back_at_ms": NOW + 20_000})), "Restarting · back in ~20 s");
        assert_eq!(says(json!({"state": "switching_account", "text": "Switching account (usage limit)"})), "Switching account (usage limit)");
        assert_eq!(says(json!({"state": "busy", "text": "Busy · 25 messages ahead of you", "queue_ahead": 25})), "Busy · 25 messages ahead of you");
        assert_eq!(
            says(json!({"state": "unresponsive", "text": "Not responding", "last_alive_ms": NOW - 4 * 60_000 - 10_000})),
            "Not responding · last sign of life 4 min ago"
        );
        assert_eq!(says(json!({"state": "unresponsive", "text": "Not responding", "last_alive_ms": NOW - 40_000})), "Not responding · last sign of life just now");
        assert_eq!(says(json!({"state": "busy", "text": "Busy", "last_alive_ms": NOW - 600_000})), "Busy", "the last sign of life belongs to unresponsive");
    }

    #[test]
    fn empty_text_gets_the_desktops_own_words() {
        assert_eq!(says(json!({"state": "busy", "queue_ahead": 25})), "Busy · 25 messages ahead of you");
        assert_eq!(says(json!({"state": "busy", "queue_ahead": 1})), "Busy · 1 message ahead of you");
        assert_eq!(says(json!({"state": "busy", "queue_ahead": 0})), "Busy");
        assert_eq!(says(json!({"state": "restarting", "text": "", "back_at_ms": NOW + 20_000})), "Restarting · back in ~20 s");
        assert_eq!(says(json!({"state": "switching_account"})), "Switching account (usage limit)");
        assert_eq!(says(json!({"state": "usage_limit"})), "Usage limit reached");
        assert_eq!(says(json!({"state": "unresponsive"})), "Not responding");
        assert_eq!(says(json!({"state": "busy", "text": "  "})), "Busy", "blank text is no text");
    }

    #[test]
    fn unknown_states_show_their_text_or_nothing() {
        assert_eq!(of(json!({"state": "napping"})).unwrap().state, State::Other("napping".into()));
        assert_eq!(words(json!({"state": "napping"})), None);
        assert_eq!(words(json!({})), None);
        assert_eq!(says(json!({"state": "napping", "text": "Taking a break", "back_at_ms": NOW + 5_000})), "Taking a break · back in ~5 s");
    }

    #[test]
    fn the_time_part_is_seconds_then_minutes_then_a_clock_time() {
        let back = |ms: i64| says(json!({"state": "restarting", "text": "Restarting", "back_at_ms": NOW + ms}));
        assert_eq!(back(19_200), "Restarting · back in ~20 s", "seconds round up");
        assert_eq!(back(400), "Restarting · back in ~1 s");
        assert_eq!(back(89_000), "Restarting · back in ~89 s");
        assert_eq!(back(90_000), "Restarting · back in ~2 min");
        assert_eq!(back(150_000), "Restarting · back in ~3 min", "minutes round to the nearest");
        assert_eq!(back(3_599_000), "Restarting · back in ~60 min");
        assert_eq!(back(3_600_000), format!("Restarting · back at {}", local(NOW + 3_600_000)));
        assert_eq!(back(0), "Restarting", "a time that is past is left out");
        assert_eq!(back(-5_000), "Restarting");
        let limit = says(json!({"state": "usage_limit", "text": "Usage limit reached", "back_at_ms": NOW + 3 * 3_600_000}));
        assert_eq!(limit, format!("Usage limit reached · back at {}", local(NOW + 3 * 3_600_000)));
        let alive = |ms: i64| says(json!({"state": "unresponsive", "last_alive_ms": NOW - ms}));
        assert_eq!(alive(119_000), "Not responding · last sign of life 1 min ago");
        assert_eq!(alive(-3_000), "Not responding · last sign of life just now", "a clock ahead never counts below zero");
    }

    #[test]
    fn times_count_on_the_hosts_clock() {
        // The Host's clock is 30 s ahead of this machine's: back_at_ms 50 s
        // ahead of the Host's now is 50 s away, not 80.
        let restarting = of(json!({"state": "restarting", "back_at_ms": NOW + 30_000 + 50_000})).unwrap();
        assert_eq!(line(&restarting, Clock { local_now_ms: NOW, host_offset_ms: 30_000 }).unwrap(), "Restarting · back in ~50 s");
        assert_eq!(line(&restarting, at(NOW)).unwrap(), "Restarting · back in ~80 s", "without an offset the local clock counts");
        // A clock time is this machine's: two minutes earlier than the Host's.
        let later = of(json!({"state": "usage_limit", "back_at_ms": NOW + 7_200_000})).unwrap();
        let skewed = Clock { local_now_ms: NOW, host_offset_ms: 120_000 };
        assert_eq!(line(&later, skewed).unwrap(), format!("Usage limit reached · back at {}", local(NOW + 7_200_000 - 120_000)));
    }

    #[test]
    fn a_host_update_is_one_line_for_every_agent() {
        let event = json!({"type": "host-updating", "text": "Host updating", "since_ms": NOW, "back_at_ms": NOW + 40_000});
        let updating = host_updating(event.as_object().unwrap());
        assert_eq!(updating.state, State::HostUpdating);
        assert_eq!(line(&updating, at(NOW)).unwrap(), "Host updating · back in ~40 s");
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

    fn header(value: Value, audience: &str) -> Option<String> {
        header_line(&of(value).expect("an availability"), audience, at(NOW))
    }

    /// Peter, 26 Nov 2026: the header shows it only while the agent cannot
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
        assert_eq!(header(json!({"state": "busy", "text": "Busy · 1 message ahead of you", "queue_ahead": 1}), "user"), None);
        assert_eq!(header(json!({"state": "busy", "queue_ahead": 1}), ""), None);
    }

    #[test]
    fn the_header_says_it_short() {
        let says = |value: Value| header(value, "agents").expect("a line");
        assert_eq!(says(json!({"state": "busy", "text": "Busy · 25 messages ahead of you", "queue_ahead": 25})), "Answering other agents · 25 ahead of you");
        assert_eq!(says(json!({"state": "busy", "text": "Busy", "queue_ahead": 0})), "Answering other agents");
        assert_eq!(says(json!({"state": "restarting", "text": "Restarting", "back_at_ms": NOW + 19_200})), "Restarting · ~20 s");
        assert_eq!(says(json!({"state": "restarting", "back_at_ms": NOW + 150_000})), "Restarting · ~3 min");
        assert_eq!(says(json!({"state": "switching_account", "text": "Switching account (usage limit)"})), "Switching account");
        assert_eq!(says(json!({"state": "switching_account", "text": "Switching account (sign-in)"})), "Switching account");
        assert_eq!(says(json!({"state": "usage_limit", "text": "Usage limit", "back_at_ms": NOW + 3 * 3_600_000})), format!("Usage limit · until {}", local(NOW + 3 * 3_600_000)));
        assert_eq!(says(json!({"state": "unresponsive", "text": "Not responding", "last_alive_ms": NOW - 4 * 60_000 - 10_000})), "No sign of life for 4 min");
        assert_eq!(says(json!({"state": "unresponsive", "last_alive_ms": NOW + 3_000})), "No sign of life for 1 min", "never zero minutes");
        assert_eq!(says(json!({"state": "unresponsive", "text": "Not responding"})), "No sign of life");
        assert_eq!(says(json!({"state": "napping", "text": "Taking a break", "back_at_ms": NOW + 5_000})), "Taking a break · ~5 s");
        assert_eq!(header(json!({"state": "napping"}), "agents"), None);
        let updating = host_updating(json!({"type": "host-updating", "text": "Host updating", "back_at_ms": NOW + 40_000}).as_object().unwrap());
        assert_eq!(header_line(&updating, "user", at(NOW)).unwrap(), "Host updating · ~40 s");
        // Restarting shows over the user's own turn: the user still waits.
        assert_eq!(header(json!({"state": "restarting", "text": "Restarting"}), "user").as_deref(), Some("Restarting"));
        // The Host's clock 30 s ahead: 50 s away, not 80.
        let restarting = of(json!({"state": "restarting", "back_at_ms": NOW + 80_000})).unwrap();
        assert_eq!(header_line(&restarting, "", Clock { local_now_ms: NOW, host_offset_ms: 30_000 }).unwrap(), "Restarting · ~50 s");
    }
}
