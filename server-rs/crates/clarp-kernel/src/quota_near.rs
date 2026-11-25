//! Whether the account an agent runs on is near a limit of its usage, before it is at it: what
//! an agent's row says of its tool then (`backend_quota` with `state: near`).
//!
//! The tools say how much of each window of their account is used (`crate::quota` for Codex,
//! `crate::standing` for Claude). A window stands near its limit from a share of it that
//! depends on how long the window is ([`near_at`]): four fifths of the five hours, nine
//! tenths of the week. At four fifths of five hours about an hour of steady work is left,
//! time to finish what is under way or to choose another tool; four fifths of a week leave
//! more than a day, and a warning that stood for a day and more would be read past, so the
//! week warns at nine tenths, when well under a day is left. The five hours' share is the
//! one the Host already tells as a warning of the five-hour window (`quota::WARNING_AT`), so
//! the row and that event agree.
//!
//! What is used of a window only grows until the window begins anew. So a window that was
//! said to be near stands until its reset has passed, however old the word is, while the
//! account is the same one; a window that says no reset stands while the word of it is fresh.
//! A window that is all used is no longer near: whether the account is used up is said apart
//! (`state: exhausted`), by its own rule.
//!
//! A warning that another account would take the turn from is noise, so the row warns only
//! where no other seat of the tool would take it ([`failover`]). A warning is for a pool near
//! its end, never for one seat while others may have room (the owner's rule, 25 Nov 2026), so
//! where the Host cannot know, it gives none ([`row`]); where it switches the seats itself and
//! its reading of them is old, it reads them first ([`needs_reading`]). A used-up account is
//! never hidden for not knowing ([`used_up_row`]).
//!
//! An account that is used up is judged by the same rule ([`used_up_failover`]), with a seat
//! taking the turn where it has any room left rather than where it is below its share: the
//! switch is the Host's business, so the row says nothing while another seat would take the
//! turn, and where none would it names the soonest moment a seat that could serve the agent's
//! model regains room. The usage screen gets a summary of each tool's pool ([`pool`]).

use serde_json::{Value, json};

use crate::quota::{from_text, moment};
use crate::recovery::Accounts;
use crate::seat_switch::{Demand, Seat};

/// From how much of a window, in percent, it stands near its limit: four fifths of the five
/// hours, nine tenths of a week (and of any other window, which is at least as long).
pub fn near_at(window: &str) -> f64 {
    match window {
        "five_hour" => FIVE_HOURS_NEAR_AT,
        _ => WEEK_NEAR_AT,
    }
}

pub const FIVE_HOURS_NEAR_AT: f64 = 80.0;
pub const WEEK_NEAR_AT: f64 = 90.0;

/// A window of an account as a tool last said it.
#[derive(Debug, Clone, Copy)]
pub struct Window<'a> {
    /// `five_hour` or `seven_day`.
    pub kind: &'a str,
    /// How much of it is used, in percent.
    pub used: f64,
    /// When it begins anew, as text; `None` where that was not said.
    pub resets_at: Option<&'a str>,
    /// Whether the word of it counts as fresh by its tool's rule.
    pub fresh: bool,
    pub observed_at_ms: i64,
}

impl Window<'_> {
    /// Whether it stands near its limit now: at or past its share and not all used, and the
    /// word of it still holds (its reset has not passed; else, without one, it is fresh).
    fn near(&self, now_ms: i64) -> bool {
        let holds = match self.resets_at.filter(|at| !at.is_empty()).map(from_text) {
            Some(Some(reset_ms)) => now_ms < reset_ms,
            _ => self.fresh,
        };
        holds && self.used >= near_at(self.kind) && self.used < 100.0
    }
}

/// What an agent's row says of a tool whose account stands near a limit, before it is known
/// whether another seat would take the turn: the most used window among those that stand
/// near (of two as used, the first given), or `None`. When it begins anew is written as
/// every moment of the row is (UTC, to the second), whatever form the tool gave it in.
pub fn nearest<'a>(
    provider: &str,
    windows: impl IntoIterator<Item = Window<'a>>,
    now_ms: i64,
) -> Option<Value> {
    let mut nearest: Option<Window<'a>> = None;
    for window in windows.into_iter().filter(|window| window.near(now_ms)) {
        if nearest.is_none_or(|kept| window.used > kept.used) {
            nearest = Some(window);
        }
    }
    let window = nearest?;
    Some(json!({
        "state": "near",
        "provider_id": provider,
        "reason": "usage_limit",
        "window": window.kind,
        "used_percentage": window.used,
        "resets_at": window.resets_at.and_then(from_text).map(moment),
        "observed_at": moment(window.observed_at_ms),
    }))
}

/// Whether another seat of a tool would take an agent's turn from an account near its limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failover {
    /// Another seat has room, and something on this Host moves the tool to it.
    Spare,
    /// None would: nothing moves the tool to another seat, or no other seat has room.
    None,
    /// This Host cannot know: another Host keeps the logins, or the reading of the seats is
    /// old, failed, or does not say enough.
    Unknown,
}

impl Failover {
    /// What the row says of it (`failover`); a spare seat is never said, the row is null.
    pub fn word(self) -> &'static str {
        match self {
            Self::Spare => "spare",
            Self::None => "none",
            Self::Unknown => "unknown",
        }
    }
}

/// From how much of a window, in percent, a seat has no room left: where less than half of
/// one percent is left, which the switcher rounds to none.
pub const ROOM_UNTIL: f64 = 99.5;

/// Whether another seat of `provider` would take the turn of an agent on `model` from an
/// account near its limit.
///
/// - `--accounts none`: nobody puts another login in place. None would.
/// - `--accounts follow`: another Host on the machine does, by seats this Host does not read.
///   Unknown.
/// - `--accounts select`: this Host does, where its pool has the owner's selector for the
///   tool (`selector`) or the switcher of the seats is on and switches by itself (`desk`, the
///   strict reading of the seats as `GET /hotseat/status` serves it). Where neither is, none
///   would. Else by the desk: unknown where its reading of the tool is old, failed or not
///   there; none where it read no account at all; spare where an account other than the one in
///   use is read as ok, with every window that bears on the agent below its share
///   ([`near_at`]: the five hours and the week, and the window of the model's family where the
///   account has one; of every family where the model's is not known) and read fresh or begun
///   anew since; none where every other account is read and none is so; unknown where one of
///   them could not be judged. `model` is the model the tool runs for the agent: its own, else
///   the tool's default.
pub fn failover(
    accounts: Accounts,
    provider: &str,
    model: &str,
    selector: bool,
    desk: &Value,
) -> Failover {
    let read = match gate(accounts, provider, selector, desk) {
        Ok(read) => read,
        Err(failover) => return failover,
    };
    let family = crate::seats::family(&json!(model));
    let bears = of_model(family);
    let served_at = desk["served_at"].as_i64().unwrap_or(0);
    let mut unjudged = false;
    let others = read["accounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|account| account["active_default"] != true);
    for account in others {
        match below_share(account, &bears, served_at) {
            Some(true) => return Failover::Spare,
            Some(false) => {}
            None => unjudged = true,
        }
    }
    if unjudged {
        Failover::Unknown
    } else {
        Failover::None
    }
}

/// Whether another seat of `provider` would take the turn of an agent on `model` from an
/// account that is used up: what the switcher itself would do (`seat_switch::decide`, the
/// choice `seats/automatic.rs` makes), on its own reading of the seats as the desk keeps it
/// (`switcher_seats`), with the seat in use taken as used up, since its tool said so. Spare
/// where it would move this model's turn to a seat now; none where it would not, with the
/// soonest moment a seat that could serve the model regains room ([`regains_at`], the seat in
/// use among them); unknown where it cannot tell (no seat is read as in use).
///
/// The rule of [`failover`] says first whether the switcher is asked at all: where the Host
/// switches no seats, none would and no moment of the pool is given; where the reading is old
/// or failed (or this Host follows another), unknown, and a row then names the reset of the
/// account in use as the tool said it. A stale reading never hides a used-up account.
pub fn used_up_failover(
    accounts: Accounts,
    provider: &str,
    model: &str,
    selector: bool,
    desk: &Value,
) -> (Failover, Option<i64>) {
    let read = match gate(accounts, provider, selector, desk) {
        Ok(read) => read,
        Err(failover) => return (failover, None),
    };
    let Some(seats) = switcher_seats(provider, desk) else {
        return (Failover::Unknown, None);
    };
    match takes(&seats, model, floor(provider, desk)) {
        Some(Some(_)) => (Failover::Spare, None),
        Some(None) => (Failover::None, regains_at(read, model, desk)),
        None => (Failover::Unknown, None),
    }
}

/// Whether the Host is to read a tool's seats before it can say whether another seat would take
/// a turn: it switches them itself (`--accounts select`, with the owner's selector for the tool
/// or the switcher's automatic lane), and its reading of them is old, failed or not there.
pub fn needs_reading(accounts: Accounts, provider: &str, selector: bool, desk: &Value) -> bool {
    accounts == Accounts::Select
        && (selector || switches(provider, desk))
        && !left_out(provider, desk)
        && fresh_read(provider, desk).is_none()
}

/// Whether the owner's tool that lists the seats leaves this tool out: nothing of it was ever
/// read, while a reading of another tool is fresh. Such a tool has no pool this Host can see:
/// its one account is its whole pool.
fn left_out(provider: &str, desk: &Value) -> bool {
    let reads = desk["providers"].as_array().into_iter().flatten();
    let mut others = reads.clone().filter(|read| read["provider"] != provider);
    let never = reads
        .clone()
        .find(|read| read["provider"] == provider)
        .is_none_or(|read| read["read_state"] == "never");
    never
        && others.any(|read| {
            let other = read["provider"].as_str().unwrap_or("");
            fresh_read(other, desk).is_some()
        })
}

/// Whether the switcher of the seats is on and moves this tool's seats by itself.
fn switches(provider: &str, desk: &Value) -> bool {
    desk["switcher"]["enabled"] == true
        && desk["switcher"]["lanes"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|lane| lane["provider"] == provider && lane["mode"] == "automatic")
}

/// Whether the switcher is asked about a tool's turns at all, and the reading it is asked on:
/// the tool's reading of the seats where it is fresh, read well and holds accounts. Else what
/// is known without it: none where nobody switches the tool's seats or no account was read;
/// unknown where another Host switches them or the reading is old, failed or not there.
fn gate<'a>(
    accounts: Accounts,
    provider: &str,
    selector: bool,
    desk: &'a Value,
) -> Result<&'a Value, Failover> {
    match accounts {
        Accounts::None => return Err(Failover::None),
        Accounts::Follow => return Err(Failover::Unknown),
        Accounts::Select => {}
    }
    if !selector && !switches(provider, desk) {
        return Err(Failover::None);
    }
    // A tool the owner's tool does not list has no pool this Host switches: its one account is
    // its whole pool, and none other would take the turn.
    if left_out(provider, desk) {
        return Err(Failover::None);
    }
    let read = fresh_read(provider, desk).ok_or(Failover::Unknown)?;
    match read["inventory"].as_str() {
        Some("ok") => Ok(read),
        Some("empty") => Err(Failover::None),
        _ => Err(Failover::Unknown),
    }
}

/// The seats of a tool as the switcher read them, in the reading the desk keeps, with the one
/// in use as the last switch made it (`seats::Seats::switcher`). The Host puts them beside
/// the desk it judges by (`switcher_seats`, never served to an app).
fn switcher_seats(provider: &str, desk: &Value) -> Option<Vec<Seat>> {
    serde_json::from_value(desk["switcher_seats"][provider].clone()).ok()
}

/// The share of a seat the switcher would rather keep, as the tool's lane says it, else the
/// switcher's own default: what it prefers among seats with room (`seats/automatic.rs`).
fn floor(provider: &str, desk: &Value) -> i64 {
    let lane = desk["switcher"]["lanes"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|lane| lane["provider"] == provider);
    let default = if provider == "claude" { 25 } else { 10 };
    lane.and_then(|lane| lane["floor_pct"].as_i64())
        .unwrap_or(default)
}

/// What the switcher would do with a turn of `model` (none named: of no model in particular)
/// were the seat in use used up: the alias of the seat it would move to (`Some(Some)`), none
/// (`Some(None)`: no seat has room for it), or `None` where it cannot tell, as no seat is read
/// as the one in use. Asked without the last switch, so its hold against moving back does not
/// count: the question is whether a seat would take the turn, not whether it moves now.
pub fn takes(seats: &[Seat], model: &str, floor: i64) -> Option<Option<String>> {
    let mut seats = seats.to_vec();
    let active = seats.iter_mut().find(|seat| seat.active)?;
    active.remaining = Some(0);
    let required: Vec<String> = Some(model)
        .filter(|model| !model.is_empty())
        .map(str::to_string)
        .into_iter()
        .collect();
    let asked = Demand {
        floor,
        required: &required,
        running: &[],
        last: &Value::Null,
        now: 0,
        cooldown: 0,
    };
    let decision = crate::seat_switch::decide(&seats, asked);
    match decision["action"].as_str() {
        Some("switch") => Some(decision["target"].as_str().map(str::to_string)),
        Some("exhausted") => Some(None),
        _ => None,
    }
}

/// The seat a tool is on, as the switcher's reading kept beside the desk says it, with the last
/// switch marked: its keyed tag, one a seat. `None` where no seat is read as in use.
pub fn seat_in_use(provider: &str, desk: &Value) -> Option<String> {
    desk["switcher_seats"][provider]
        .as_array()?
        .iter()
        .find(|seat| seat["active"] == true)?["tag"]
        .as_str()
        .map(str::to_string)
}

/// Whether a seat has room for a turn of no model in particular, as the switcher reads it: some
/// of its five hours left, and of its week unless it may run over (`overage`). `None` where its
/// five hours could not be read.
fn has_room(seat: &Seat) -> Option<bool> {
    let left = seat.remaining?;
    let week_out = seat.secondary.is_some_and(|week| week <= 0) && !seat.overage;
    Some(left > 0 && !week_out)
}

/// The desk's reading of a tool's seats where it is fresh and was read well; `None` where it
/// is old, failed or not there, which says nothing of whether a seat has room now.
fn fresh_read<'a>(provider: &str, desk: &'a Value) -> Option<&'a Value> {
    desk["providers"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|read| read["provider"] == provider)
        .filter(|read| read["stale"] == false && read["read_state"] == "ok")
}

/// Which windows of a seat bear on a turn of a model's family: the five hours and the week,
/// and the window of the family where the seat has one; of every family where the model's is
/// not known.
fn of_model(family: Option<&str>) -> impl Fn(&str) -> bool {
    move |kind: &str| match kind.strip_prefix("model:") {
        Some(of) => family.is_none_or(|family| of == family),
        None => matches!(kind, "5h" | "7d"),
    }
}

/// Which windows of a seat bear on whether it has room at all, of no model in particular: the
/// five hours and the week.
fn general(kind: &str) -> bool {
    matches!(kind, "5h" | "7d")
}

/// Whether a window of the desk began anew since it was read: it has no number, and its reset
/// has passed.
fn begun_anew(window: &Value, served_at: i64) -> bool {
    window["used_ratio"].is_null()
        && window["resets_at"]
            .as_i64()
            .is_some_and(|reset| reset <= served_at)
}

/// What is used of a window of the desk, as a share of one: nothing where it began anew since
/// it was read; `None` where that cannot be told (old, or no number).
fn used_of(window: &Value, served_at: i64) -> Option<f64> {
    if begun_anew(window, served_at) {
        return Some(0.0);
    }
    window["used_ratio"]
        .as_f64()
        .filter(|_| window["stale"] == false)
}

/// Whether an account of the desk is below the near share in every window that bears on the
/// turn: `None` where it cannot be told (its state is not known, or a window that bears on it is
/// old or has no number, and none is known to be at its share). Whatever order the windows come
/// in, one at its share says no.
fn below_share(account: &Value, bears: &dyn Fn(&str) -> bool, served_at: i64) -> Option<bool> {
    match account["state"].as_str() {
        Some("ok") => {}
        Some("exhausted" | "auth_error") => return Some(false),
        _ => return None,
    }
    let (mut general_read, mut unread) = (false, false);
    for window in account["windows"].as_array().into_iter().flatten() {
        let kind = window["kind"].as_str().unwrap_or("");
        if !bears(kind) {
            continue;
        }
        let Some(used) = used_of(window, served_at) else {
            unread = true;
            continue;
        };
        let share = if kind == "5h" {
            near_at("five_hour")
        } else {
            near_at("seven_day")
        };
        if used * 100.0 >= share {
            return Some(false);
        }
        general_read |= general(kind);
    }
    if unread {
        return None;
    }
    general_read.then_some(true)
}

/// When an account of the desk that has no room regains it: when the last of its windows that
/// bear on the turn and are all used begins anew. A window read as all used counts by its reset
/// however old the reading is, since its reset was set in advance; one of an account that was
/// used up and began anew since counts at the reset that passed, where it passed after the
/// reading (`observed_at`): the account regained room then, and the Host reads the seats again
/// to see it. `None` where that is not known (a full window
/// that does not say when, an account used up with no window read as full) and for an account
/// that is signed out, which no reset frees.
fn regains(
    account: &Value,
    bears: &dyn Fn(&str) -> bool,
    (served_at, observed_at): (i64, i64),
) -> Option<i64> {
    if account["state"] == "auth_error" {
        return None;
    }
    let was_out = account["state"] != "ok";
    let mut last: Option<i64> = None;
    for window in account["windows"].as_array().into_iter().flatten() {
        let kind = window["kind"].as_str().unwrap_or("");
        let full = window["used_ratio"]
            .as_f64()
            .is_some_and(|used| used * 100.0 >= ROOM_UNTIL);
        // A reset that passed counts only where it passed after the reading the seat is judged
        // by: a reading taken since that still shows no room says the reset did not free it
        // (a seat whose row keeps failing carries windows that reset long ago).
        let regained = was_out
            && begun_anew(window, served_at)
            && window["resets_at"]
                .as_i64()
                .is_some_and(|reset| reset > observed_at);
        if !bears(kind) || !(full || regained) {
            continue;
        }
        let reset = window["resets_at"].as_i64()?;
        last = Some(last.map_or(reset, |last| last.max(reset)));
    }
    last
}

/// The soonest moment a seat of a reading that could serve `model` regains room, the seat in
/// use among them; `None` where no seat says when. A moment that passed is the moment a seat
/// regained room that the reading does not show yet.
fn regains_at(read: &Value, model: &str, desk: &Value) -> Option<i64> {
    let bears = of_model(crate::seats::family(&json!(model)));
    let served_at = desk["served_at"].as_i64().unwrap_or(0);
    let observed_at = read["observed_at"].as_i64().unwrap_or(i64::MAX);
    read["accounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|account| regains(account, &bears, (served_at, observed_at)))
        .min()
}

/// What the usage screen says of a tool's pool of seats (`pool` of a provider in
/// `GET /backend-usage`), from the desk as `GET /hotseat/status` serves it and the switcher's own
/// reading beside it; `None` where the desk read no account of the tool. Of no model in
/// particular: how many seats there are, how many have room for a turn and how many are used
/// up as the switcher reads them ([`has_room`]), the alias of the one in use and of the one the
/// switcher would move to were it used up ([`takes`]), the soonest moment a used-up seat
/// regains room, and where every seat is used up, when the first does; and the last switch of
/// the tool's seats this Host made (`switched`: from which seat to which, and when). A seat
/// that cannot be read is counted in neither.
///
/// Where the reading is old or failed (`stale`), no seat is counted as having room or as used
/// up, none is named next, and nothing says that every seat is used up: an old reading says
/// nothing of now. The moments it holds are still given, since a reset is set in advance.
pub fn pool(provider: &str, desk: &Value) -> Option<Value> {
    let read = desk["providers"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|read| read["provider"] == provider)?;
    let accounts = read["accounts"].as_array().filter(|all| !all.is_empty())?;
    if read["inventory"] != "ok" {
        return None;
    }
    let seats = switcher_seats(provider, desk).filter(|_| fresh_read(provider, desk).is_some());
    let served_at = desk["served_at"].as_i64().unwrap_or(0);
    let observed_at = read["observed_at"].as_i64().unwrap_or(i64::MAX);
    let next = accounts
        .iter()
        .filter_map(|account| regains(account, &general, (served_at, observed_at)))
        .min();
    let (mut with_room, mut used_up, mut unread) = (0_usize, 0_usize, false);
    for seat in seats.iter().flatten() {
        match has_room(seat) {
            Some(true) => with_room += 1,
            Some(false) => used_up += 1,
            None => unread = true,
        }
    }
    let next_alias = seats
        .as_ref()
        .and_then(|seats| takes(seats, "", floor(provider, desk)))
        .flatten()
        .map(|alias| crate::seats::display_alias(&json!(alias)));
    let active = accounts
        .iter()
        .find(|account| account["active_default"] == true)
        .and_then(|account| account["alias"].as_str());
    let fresh = seats.is_some();
    let all_used_up = fresh && with_room == 0 && !unread;
    // The last switch of the tool's seats this Host made, by its switcher or by hand.
    let switched = desk["actions"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|action| action["provider"] == provider && action["kind"] == "switched")
        .map(|action| {
            json!({
                "from_alias": action["from_alias"], "to_alias": action["to_alias"],
                "at": action["at"].as_i64().map(moment),
            })
        });
    let at = |ms: Option<i64>| ms.map(moment);
    Some(json!({
        "seats": accounts.len(),
        "with_room": fresh.then_some(with_room),
        "used_up": fresh.then_some(used_up),
        "active_alias": active,
        "next_alias": next_alias,
        "switched": switched,
        "next_reset_at": at(next),
        "regains_room_at": at(next.filter(|_| all_used_up)),
        "stale": !fresh,
        "observed_at": at(read["observed_at"].as_i64()),
    }))
}

/// Until when a judgement of a tool's pool from this desk holds: until the desk's reading of
/// the tool grows old by its own rule (twice its cadence, and ten minutes at least), the moment
/// after which it says nothing of now. `None` where the tool's seats were not read.
pub fn fresh_until(provider: &str, desk: &Value) -> Option<i64> {
    let read = fresh_read(provider, desk)?;
    let observed_at = read["observed_at"].as_i64()?;
    Some(observed_at + crate::seats::stale_after_ms(read["cadence_s"].as_i64()))
}

/// A `provider-limit` event of a tool's account, given whether another seat of the tool would
/// take the turn ([`used_up_failover`] for the tool's default model): an account that reached
/// its limit while another seat has room is the switch's business, so its `hard_limit` is not
/// told (`None`); else it is told with `failover`, and where no seat had room, when the first
/// regains it (`pool_regains_at`, UTC to the second; null where that is not known). Every
/// other event, a warning, a recovery or a limit no longer known, is told as it is.
pub fn limit_event(event: Value, failover: Failover, regains_at: Option<i64>) -> Option<Value> {
    if event["type"] != "provider-limit" || event["kind"] != "hard_limit" {
        return Some(event);
    }
    if failover == Failover::Spare {
        return None;
    }
    let mut event = event;
    event["failover"] = json!(failover.word());
    event["pool_regains_at"] = json!(regains_at.map(moment));
    Some(event)
}

/// A `provider-limit` warning of a tool's account (a window near its limit), given whether another
/// seat of the tool would take the turn from an account near its limit ([`failover`], the rule
/// of the agents' near rows): a warning is for a pool near its end, so it is told only where no
/// seat would, with `failover: "none"`; not where one would, nor where that cannot be known
/// (`None`).
pub fn warning_event(event: Value, near: Failover) -> Option<Value> {
    if near != Failover::None {
        return None;
    }
    let mut event = event;
    event["failover"] = json!(near.word());
    Some(event)
}

/// What an agent's row says of a tool near its limit, given whether another seat would take
/// the turn: null where one would, and where that cannot be known.
pub fn row(near: &Value, failover: Failover) -> Value {
    // A warning is for a pool near its end, never for one seat while others may have room: where
    // it cannot be known whether another seat would take the turn, nothing is said (a used-up
    // account stands all the same, [`used_up_row`]).
    if matches!(failover, Failover::Spare | Failover::Unknown) {
        return Value::Null;
    }
    let mut row = near.clone();
    row["failover"] = json!(failover.word());
    row
}

/// What an agent's row says of a tool whose account in use is used up, given whether another
/// seat would take the turn ([`used_up_failover`]): null where one would, since the switch is
/// the Host's business; else the row as the tool said it, with `failover`, and with the pool's
/// soonest moment a seat regains room where that is sooner than the reset the tool named.
pub fn used_up_row(used_up: &Value, failover: Failover, regains_at: Option<i64>) -> Value {
    if failover == Failover::Spare {
        return Value::Null;
    }
    let mut row = used_up.clone();
    row["failover"] = json!(failover.word());
    let named = row["resets_at"].as_str().and_then(from_text);
    if let Some(at) = regains_at.filter(|at| named.is_none_or(|named| *at < named)) {
        row["resets_at"] = json!(moment(at));
    }
    row
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    const NOW: i64 = 1_900_000_000_000;

    fn window<'a>(kind: &'a str, used: f64, resets_at: Option<&'a str>, fresh: bool) -> Window<'a> {
        Window {
            kind,
            used,
            resets_at,
            fresh,
            observed_at_ms: NOW - 60_000,
        }
    }

    #[test]
    fn a_window_is_near_from_its_share_until_it_is_all_used() {
        let later = Some("2030-03-17T19:00:00Z");
        assert_eq!(moment(NOW), "2030-03-17T17:46:40Z");
        // Below the five hours' share, at it, and all used.
        assert!(nearest("codex", [window("five_hour", 79.9, later, true)], NOW).is_none());
        let at = nearest("codex", [window("five_hour", 80.0, later, true)], NOW).unwrap();
        assert_eq!(
            at,
            json!({
                "state": "near", "provider_id": "codex", "reason": "usage_limit",
                "window": "five_hour", "used_percentage": 80.0,
                "resets_at": "2030-03-17T19:00:00Z", "observed_at": "2030-03-17T17:45:40Z",
            })
        );
        assert!(nearest("codex", [window("five_hour", 100.0, later, true)], NOW).is_none());
        // The week warns later: at 85 it does not, at 90 it does.
        assert!(nearest("claude", [window("seven_day", 85.0, later, true)], NOW).is_none());
        let week = nearest("claude", [window("seven_day", 90.0, later, true)], NOW).unwrap();
        assert_eq!(week["window"], "seven_day");
        // Of two near, the more used.
        let both = [
            window("five_hour", 82.0, later, true),
            window("seven_day", 95.5, Some("2030-03-20T00:00:00Z"), true),
        ];
        let named = nearest("claude", both, NOW).unwrap();
        assert_eq!(
            (named["window"].as_str(), named["used_percentage"].as_f64()),
            (Some("seven_day"), Some(95.5))
        );
    }

    #[test]
    fn a_near_window_stands_until_it_begins_anew() {
        // Old words of a window whose reset is still ahead stand: what is used only grows.
        let ahead = window("five_hour", 90.0, Some("2030-03-17T19:00:00Z"), false);
        assert!(nearest("codex", [ahead], NOW).is_some());
        // Its reset passed: it is no longer near, fresh or not.
        let passed = window("five_hour", 90.0, Some("2030-03-17T17:46:40Z"), true);
        assert!(nearest("codex", [passed], NOW).is_none());
        // No reset said: it stands while the word is fresh.
        for unsaid in [None, Some("")] {
            assert!(nearest("claude", [window("five_hour", 90.0, unsaid, true)], NOW).is_some());
            assert!(nearest("claude", [window("five_hour", 90.0, unsaid, false)], NOW).is_none());
            let row = nearest("claude", [window("five_hour", 90.0, unsaid, true)], NOW).unwrap();
            assert!(row["resets_at"].is_null());
        }
    }

    fn desk(enabled: bool, mode: &str, read: Value) -> Value {
        json!({
            "switcher": { "enabled": enabled, "lanes": [{ "provider": "claude", "mode": mode }] },
            "providers": [read],
        })
    }

    fn read(accounts: Value) -> Value {
        json!({
            "provider": "claude", "inventory": "ok", "stale": false, "read_state": "ok",
            "accounts": accounts,
        })
    }

    fn seat(active: bool, state: &str, windows: &[(&str, Option<f64>, bool)]) -> Value {
        let windows: Vec<Value> = windows
            .iter()
            .map(|(kind, used, stale)| json!({ "kind": kind, "used_ratio": used, "stale": stale }))
            .collect();
        json!({ "active_default": active, "state": state, "windows": windows })
    }

    #[test]
    fn another_seat_takes_the_turn_only_where_the_host_moves_to_it_and_it_has_room() {
        let in_use = seat(
            true,
            "ok",
            &[("5h", Some(0.85), false), ("7d", Some(0.2), false)],
        );
        let spare = seat(
            false,
            "ok",
            &[("5h", Some(0.1), false), ("7d", Some(0.3), false)],
        );
        let full = seat(
            false,
            "ok",
            &[("5h", Some(0.81), false), ("7d", Some(0.3), false)],
        );
        let week = seat(
            false,
            "ok",
            &[("5h", Some(0.1), false), ("7d", Some(0.9), false)],
        );
        let out = seat(false, "exhausted", &[]);
        let unread = seat(false, "unknown", &[]);
        let old = seat(
            false,
            "ok",
            &[("5h", Some(0.1), true), ("7d", Some(0.3), false)],
        );
        let select = |desk: &Value, selector: bool| {
            failover(
                Accounts::Select,
                "claude",
                "claude-opus-5-5",
                selector,
                desk,
            )
        };
        let with = |accounts: Value| desk(true, "automatic", read(accounts));
        // The modes that say it without a desk.
        let any = with(json!([in_use, spare]));
        assert_eq!(
            failover(Accounts::None, "claude", "", true, &any),
            Failover::None
        );
        assert_eq!(
            failover(Accounts::Follow, "claude", "", true, &any),
            Failover::Unknown
        );
        // A seat with room, and the switcher moves to it, or the pool's selector does (with a
        // selector the desk is on, whatever the switcher's lane says).
        assert_eq!(select(&any, false), Failover::Spare);
        assert_eq!(
            select(&desk(true, "notify", read(json!([in_use, spare]))), true),
            Failover::Spare
        );
        // Nothing moves the tool: none would, whatever seat has room.
        assert_eq!(
            select(
                &desk(false, "automatic", read(json!([in_use, spare]))),
                false
            ),
            Failover::None
        );
        assert_eq!(
            select(&desk(true, "notify", read(json!([in_use, spare]))), false),
            Failover::None
        );
        // No other seat has room: near its own share, out, or signed out.
        assert_eq!(
            select(&with(json!([in_use, full, week, out])), false),
            Failover::None
        );
        assert_eq!(select(&with(json!([in_use])), false), Failover::None);
        // One that cannot be judged: unknown, unless another has room.
        assert_eq!(
            select(&with(json!([in_use, full, unread])), false),
            Failover::Unknown
        );
        assert_eq!(
            select(&with(json!([in_use, old])), false),
            Failover::Unknown
        );
        assert_eq!(
            select(&with(json!([in_use, unread, spare])), false),
            Failover::Spare
        );
        // A reading that is old, failed or not there: unknown.
        let mut stale = read(json!([in_use, spare]));
        stale["stale"] = json!(true);
        assert_eq!(
            select(&desk(true, "automatic", stale), false),
            Failover::Unknown
        );
        let mut failed = read(json!([in_use, spare]));
        failed["read_state"] = json!("failed");
        assert_eq!(
            select(&desk(true, "automatic", failed), false),
            Failover::Unknown
        );
        assert_eq!(select(&json!({}), true), Failover::Unknown);
        // A reading of no account at all: no other seat.
        let mut empty = read(json!([]));
        empty["inventory"] = json!("empty");
        assert_eq!(
            select(&desk(true, "automatic", empty), false),
            Failover::None
        );
    }

    #[test]
    fn a_seat_whose_window_began_anew_since_it_was_read_has_room() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        // As the desk serves a window whose reset passed since it was read: no number, stale.
        let anew = json!({ "active_default": false, "state": "ok", "windows": [
            { "kind": "5h", "used_ratio": null, "stale": true, "resets_at": NOW - 1_000 },
            { "kind": "7d", "used_ratio": 0.3, "stale": false, "resets_at": NOW + 1_000_000 },
        ]});
        let mut served = desk(true, "automatic", read(json!([in_use, anew])));
        served["served_at"] = json!(NOW);
        let of = failover(Accounts::Select, "claude", "", false, &served);
        assert_eq!(of, Failover::Spare);
        // Not yet begun anew, and no number: it cannot be judged.
        served["served_at"] = json!(NOW - 2_000);
        let of = failover(Accounts::Select, "claude", "", false, &served);
        assert_eq!(of, Failover::Unknown);
    }

    #[test]
    fn a_seat_blocked_for_the_agents_model_takes_none_of_its_turns() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        let no_opus = seat(
            false,
            "ok",
            &[
                ("5h", Some(0.1), false),
                ("7d", Some(0.3), false),
                ("model:opus", Some(0.95), false),
            ],
        );
        let desk = desk(true, "automatic", read(json!([in_use, no_opus])));
        let of = |model: &str| failover(Accounts::Select, "claude", model, false, &desk);
        assert_eq!(of("claude-opus-5-5"), Failover::None);
        assert_eq!(of("claude-sonnet-5-5"), Failover::Spare);
        // A model of no family that is known: every family's window bears on it.
        assert_eq!(of(""), Failover::None);
        assert_eq!(of("some-model"), Failover::None);
    }

    #[test]
    fn the_row_says_whether_failover_was_known() {
        let near = nearest(
            "codex",
            [window(
                "five_hour",
                84.0,
                Some("2030-03-17T19:00:00Z"),
                true,
            )],
            NOW,
        )
        .unwrap();
        assert!(row(&near, Failover::Spare).is_null());
        assert_eq!(row(&near, Failover::None)["failover"], "none");
        // Not known whether another seat would take it: no warning.
        assert!(row(&near, Failover::Unknown).is_null());
        assert_eq!(row(&near, Failover::None)["used_percentage"], 84.0);
        // A reset the tool gave with parts of a second or another offset is written in UTC to
        // the second.
        let odd = window(
            "five_hour",
            84.0,
            Some("2030-03-17T21:00:00.597645+02:00"),
            true,
        );
        let near = nearest("claude", [odd], NOW).unwrap();
        assert_eq!(near["resets_at"], "2030-03-17T19:00:00Z");
    }

    /// A seat whose windows say when they begin anew: (kind, used, resets_at).
    fn timed(alias: &str, active: bool, state: &str, windows: &[(&str, f64, i64)]) -> Value {
        let windows: Vec<Value> = windows
            .iter()
            .map(|(kind, used, resets)| {
                json!({ "kind": kind, "used_ratio": used, "stale": false, "resets_at": resets })
            })
            .collect();
        json!({ "alias": alias, "active_default": active, "state": state, "windows": windows })
    }

    fn served(accounts: Value) -> Value {
        let mut read = read(accounts);
        read["observed_at"] = json!(NOW - 60_000);
        let mut desk = desk(true, "automatic", read);
        desk["served_at"] = json!(NOW);
        desk
    }

    /// A seat as the switcher reads it: what is left of its five hours and its week, in percent.
    fn sw(alias: &str, active: bool, left: Option<i64>, week: Option<i64>) -> Value {
        json!({ "alias": alias, "active": active, "remaining": left, "secondary": week })
    }

    /// The desk with the switcher's reading of Claude's seats beside it.
    fn judged_by(mut desk: Value, seats: Value) -> Value {
        desk["switcher_seats"]["claude"] = seats;
        desk
    }

    const HOUR: i64 = 3_600_000;

    #[test]
    fn a_used_up_account_says_nothing_where_the_switcher_would_move_the_turn() {
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 2 * HOUR)]);
        // At 92% of its five hours: too near to spare a warning, room enough to take a turn.
        let nearly = timed(
            "nearly",
            false,
            "ok",
            &[("5h", 0.92, NOW + HOUR), ("7d", 0.4, NOW + 90 * HOUR)],
        );
        let seats = json!([
            sw("work", true, Some(0), Some(60)),
            sw("nearly", false, Some(8), Some(60))
        ]);
        let desk = judged_by(served(json!([in_use, nearly])), seats);
        let model = "claude-opus-5-5";
        assert_eq!(
            failover(Accounts::Select, "claude", model, false, &desk),
            Failover::None
        );
        let (of, regains) = used_up_failover(Accounts::Select, "claude", model, false, &desk);
        assert_eq!((of, regains), (Failover::Spare, None));
        let row = json!({ "state": "exhausted", "resets_at": moment(NOW + 2 * HOUR) });
        assert!(used_up_row(&row, Failover::Spare, None).is_null());
        // No switcher's reading beside the desk: it cannot be told.
        let bare = served(json!([in_use, nearly]));
        assert_eq!(
            used_up_failover(Accounts::Select, "claude", model, false, &bare).0,
            Failover::Unknown
        );
    }

    #[test]
    fn the_switchers_own_reading_decides_what_the_desks_windows_do_not_show() {
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 2 * HOUR)]);
        let roomy = |alias: &str| {
            timed(
                alias,
                false,
                "ok",
                &[("5h", 0.1, NOW + HOUR), ("7d", 0.2, NOW + 90 * HOUR)],
            )
        };
        let desk = served(json!([in_use, roomy("spare")]));
        let of = |seats: Value, model: &str| {
            let desk = judged_by(desk.clone(), seats);
            used_up_failover(Accounts::Select, "claude", model, true, &desk).0
        };
        let work = sw("work", true, Some(0), Some(60));
        // Not available, as the tool said: the switcher reads it as without room.
        let unavailable = json!([work, sw("spare", false, Some(0), Some(80))]);
        assert_eq!(of(unavailable, ""), Failover::None);
        // Closed to Opus: Opus's turn stays, Sonnet's moves.
        let mut blocked = sw("spare", false, Some(90), Some(80));
        blocked["blocked_models"] = json!(["opus"]);
        let closed = json!([work, blocked]);
        assert_eq!(of(closed.clone(), "claude-opus-5-5"), Failover::None);
        assert_eq!(of(closed, "claude-sonnet-5-5"), Failover::Spare);
        // Its week is used, but it may run over: the switcher takes it.
        let mut over = sw("spare", false, Some(90), Some(0));
        over["overage"] = json!(true);
        assert_eq!(of(json!([work, over]), ""), Failover::Spare);
        let over_not = json!([work, sw("spare", false, Some(90), Some(0))]);
        assert_eq!(of(over_not, ""), Failover::None);
        // Read as degraded: of no family can it be told that it has room.
        let mut degraded = sw("spare", false, Some(90), Some(80));
        degraded["degraded"] = json!(true);
        assert_eq!(
            of(json!([work, degraded]), "claude-opus-5-5"),
            Failover::None
        );
        // No seat read as the one in use: it cannot be told.
        let none_active = json!([
            sw("work", false, Some(0), Some(60)),
            sw("spare", false, Some(90), Some(80))
        ]);
        assert_eq!(of(none_active, ""), Failover::Unknown);
    }

    #[test]
    fn where_every_seat_is_used_up_the_row_names_the_soonest_a_seat_for_the_model_regains_room() {
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 3 * HOUR)]);
        // One seat back in an hour, but its Opus week is full for four days; one back in two
        // hours for every model; one signed out.
        let no_opus = timed(
            "no-opus",
            false,
            "exhausted",
            &[
                ("5h", 1.0, NOW + HOUR),
                ("model:opus", 1.0, NOW + 96 * HOUR),
            ],
        );
        let soon = timed(
            "soon",
            false,
            "exhausted",
            &[("5h", 1.0, NOW + 2 * HOUR), ("7d", 0.5, NOW + 90 * HOUR)],
        );
        let out = timed("out", false, "auth_error", &[]);
        let seats = json!([
            sw("work", true, Some(0), Some(50)),
            sw("no-opus", false, Some(0), Some(50)),
            sw("soon", false, Some(0), Some(50)),
            sw("out", false, None, None),
        ]);
        let desk = judged_by(served(json!([in_use, no_opus, soon, out])), seats);
        let of = |model: &str| used_up_failover(Accounts::Select, "claude", model, true, &desk);
        assert_eq!(
            of("claude-opus-5-5"),
            (Failover::None, Some(NOW + 2 * HOUR))
        );
        assert_eq!(of("claude-sonnet-5-5"), (Failover::None, Some(NOW + HOUR)));
        let said = json!({
            "state": "exhausted", "provider_id": "claude", "reason": "usage_limit",
            "window": "five_hour", "used_percentage": 100.0, "resets_at": moment(NOW + 3 * HOUR),
        });
        let row = used_up_row(&said, Failover::None, Some(NOW + 2 * HOUR));
        assert_eq!(
            (
                &row["state"],
                &row["failover"],
                &row["resets_at"],
                &row["window"]
            ),
            (
                &json!("exhausted"),
                &json!("none"),
                &json!(moment(NOW + 2 * HOUR)),
                &json!("five_hour")
            )
        );
        // A moment of the pool later than the tool's own reset leaves the tool's.
        let row = used_up_row(&said, Failover::None, Some(NOW + 5 * HOUR));
        assert_eq!(row["resets_at"], json!(moment(NOW + 3 * HOUR)));
    }

    #[test]
    fn at_the_moment_a_seat_regains_room_the_row_keeps_that_moment_and_never_a_later_one() {
        // The seat `soon` was used up until NOW - 1s; the reading was taken before, so the desk
        // serves it as no longer known, its window without a number. A stale full week of
        // another seat still counts by its reset.
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 3 * HOUR)]);
        let soon = json!({ "alias": "soon", "active_default": false, "state": "unknown", "windows": [
            { "kind": "5h", "used_ratio": null, "stale": true, "resets_at": NOW - 1_000 },
        ]});
        let week = json!({ "alias": "week", "active_default": false, "state": "exhausted", "windows": [
            { "kind": "5h", "used_ratio": 1.0, "stale": false, "resets_at": NOW + HOUR },
            { "kind": "7d", "used_ratio": 1.0, "stale": true, "resets_at": NOW + 40 * HOUR },
        ]});
        let seats = json!([
            sw("work", true, Some(0), Some(50)),
            sw("soon", false, Some(0), Some(50)),
            sw("week", false, Some(0), Some(0)),
        ]);
        let desk = judged_by(served(json!([in_use, soon, week])), seats);
        let (of, regains) = used_up_failover(Accounts::Select, "claude", "", true, &desk);
        assert_eq!((of, regains), (Failover::None, Some(NOW - 1_000)));
        // Of `week` alone: its last full window, the stale week, not the five hours.
        let desk = judged_by(
            served(json!([timed("work", true, "exhausted", &[]), week])),
            json!([
                sw("work", true, Some(0), Some(50)),
                sw("week", false, Some(0), Some(0))
            ]),
        );
        let (_, regains) = used_up_failover(Accounts::Select, "claude", "", true, &desk);
        assert_eq!(regains, Some(NOW + 40 * HOUR));
        // A seat whose row keeps failing carries windows that reset days before this reading:
        // that moment is no answer, and the row names the next real one.
        let failing = json!({ "alias": "failing", "active_default": false, "state": "unknown", "windows": [
            { "kind": "5h", "used_ratio": null, "stale": true, "resets_at": NOW - 48 * HOUR },
        ]});
        let desk = judged_by(
            served(json!([in_use, failing])),
            json!([
                sw("work", true, Some(0), Some(50)),
                sw("failing", false, None, None)
            ]),
        );
        let (_, regains) = used_up_failover(Accounts::Select, "claude", "", true, &desk);
        assert_eq!(regains, Some(NOW + 3 * HOUR));
        let summary = pool("claude", &desk).unwrap();
        assert_eq!(summary["next_reset_at"], json!(moment(NOW + 3 * HOUR)));
        // Beside it, a seat that really regained room since the reading: its moment is the
        // row's, so the Host reads the seats again for it, and the failing seat's old one does
        // not stand in its way.
        let desk = judged_by(
            served(json!([in_use, failing, soon])),
            json!([
                sw("work", true, Some(0), Some(50)),
                sw("failing", false, None, None),
                sw("soon", false, Some(0), Some(50)),
            ]),
        );
        let (_, regains) = used_up_failover(Accounts::Select, "claude", "", true, &desk);
        assert_eq!(regains, Some(NOW - 1_000));
    }

    #[test]
    fn a_near_seat_is_judged_alike_whatever_order_its_windows_come_in() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        let full_then_old = seat(
            false,
            "ok",
            &[("5h", Some(0.9), false), ("7d", Some(0.1), true)],
        );
        let old_then_full = seat(
            false,
            "ok",
            &[("7d", Some(0.1), true), ("5h", Some(0.9), false)],
        );
        for other in [full_then_old, old_then_full] {
            let desk = desk(true, "automatic", read(json!([in_use, other])));
            assert_eq!(
                failover(Accounts::Select, "claude", "", false, &desk),
                Failover::None
            );
        }
    }

    #[test]
    fn a_tool_without_a_pool_says_its_used_up_account_as_before() {
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 3 * HOUR)]);
        let spare = timed(
            "spare",
            false,
            "ok",
            &[("5h", 0.1, NOW + HOUR), ("7d", 0.1, NOW + HOUR)],
        );
        let seats = json!([
            sw("work", true, Some(0), Some(60)),
            sw("spare", false, Some(90), Some(90))
        ]);
        let desk = judged_by(served(json!([in_use, spare])), seats);
        // Nothing moves the tool, or nobody switches logins: the row stands, its reset its own.
        let no_switcher = {
            let mut desk = desk.clone();
            desk["switcher"]["enabled"] = json!(false);
            desk
        };
        for (accounts, desk) in [(Accounts::Select, &no_switcher), (Accounts::None, &desk)] {
            assert_eq!(
                used_up_failover(accounts, "claude", "", false, desk),
                (Failover::None, None)
            );
        }
        // Beside another Host that switches: not this Host's to know.
        assert_eq!(
            used_up_failover(Accounts::Follow, "claude", "", true, &desk),
            (Failover::Unknown, None)
        );
        let said = json!({ "state": "exhausted", "resets_at": moment(NOW + 3 * HOUR) });
        let row = used_up_row(&said, Failover::None, None);
        assert_eq!(row["resets_at"], said["resets_at"]);
        assert_eq!(row["failover"], "none");
    }

    #[test]
    fn a_stale_reading_never_hides_a_used_up_account() {
        let in_use = timed("work", true, "exhausted", &[("5h", 1.0, NOW + 3 * HOUR)]);
        let spare = timed(
            "spare",
            false,
            "ok",
            &[("5h", 0.1, NOW + HOUR), ("7d", 0.1, NOW + HOUR)],
        );
        let seats = json!([
            sw("work", true, Some(0), Some(60)),
            sw("spare", false, Some(90), Some(90))
        ]);
        let mut desk = judged_by(served(json!([in_use, spare])), seats);
        desk["providers"][0]["stale"] = json!(true);
        let (of, regains) = used_up_failover(Accounts::Select, "claude", "", true, &desk);
        assert_eq!((of, regains), (Failover::Unknown, None));
        let said = json!({ "state": "exhausted", "resets_at": moment(NOW + 3 * HOUR) });
        let row = used_up_row(&said, of, regains);
        assert_eq!(
            (&row["state"], &row["failover"], &row["resets_at"]),
            (&json!("exhausted"), &json!("unknown"), &said["resets_at"])
        );
        // Nor does it claim room in the pool's summary, or name a seat to move to.
        let summary = pool("claude", &desk).unwrap();
        assert_eq!(
            (
                &summary["stale"],
                &summary["with_room"],
                &summary["regains_room_at"],
                &summary["next_alias"]
            ),
            (&json!(true), &Value::Null, &Value::Null, &Value::Null)
        );
        assert_eq!(summary["seats"], 2);
    }

    #[test]
    fn the_pool_summary_counts_seats_as_the_switcher_reads_them_and_says_when_one_regains_room() {
        let in_use = timed(
            "work",
            true,
            "exhausted",
            &[("5h", 1.0, NOW + 3 * HOUR), ("7d", 0.2, NOW + 90 * HOUR)],
        );
        let spare = timed(
            "spare",
            false,
            "ok",
            &[("5h", 0.1, NOW + HOUR), ("7d", 0.1, NOW + 90 * HOUR)],
        );
        let week = timed(
            "week",
            false,
            "ok",
            &[("5h", 0.2, NOW + HOUR), ("7d", 1.0, NOW + 40 * HOUR)],
        );
        let out = timed("out", false, "auth_error", &[]);
        let accounts = json!([in_use, spare, week, out]);
        let seats = json!([
            sw("work", true, Some(0), Some(80)),
            sw("spare", false, Some(90), Some(90)),
            sw("week", false, Some(80), Some(0)),
            sw("out", false, None, None),
        ]);
        let summary = pool("claude", &judged_by(served(accounts.clone()), seats)).unwrap();
        assert_eq!(
            summary,
            json!({
                "seats": 4, "with_room": 1, "used_up": 2, "active_alias": "work",
                "next_alias": "spare", "switched": null, "next_reset_at": moment(NOW + 3 * HOUR),
                "regains_room_at": null, "stale": false, "observed_at": moment(NOW - 60_000),
            })
        );
        // A switch this Host made: from which seat to which, and when.
        let mut desk = judged_by(
            served(accounts.clone()),
            json!([sw("work", true, Some(0), Some(80))]),
        );
        desk["actions"] = json!([
            { "provider": "codex", "kind": "switched", "at": NOW, "from_alias": "x", "to_alias": "y" },
            { "provider": "claude", "kind": "switched", "at": NOW - 60_000, "from_alias": "old", "to_alias": "work" },
        ]);
        assert_eq!(
            pool("claude", &desk).unwrap()["switched"],
            json!({ "from_alias": "old", "to_alias": "work", "at": moment(NOW - 60_000) })
        );
        // Of two seats with room, the switcher's choice: more of the five hours left.
        let roomier = timed("roomier", false, "ok", &[("5h", 0.05, NOW + HOUR)]);
        let seats = json!([
            sw("work", true, Some(0), Some(80)),
            sw("spare", false, Some(90), Some(90)),
            sw("roomier", false, Some(95), Some(60)),
        ]);
        let desk = judged_by(served(json!([accounts[0], accounts[1], roomier])), seats);
        assert_eq!(pool("claude", &desk).unwrap()["next_alias"], "roomier");
        // Every seat used up and read: when the first regains room, and no seat to move to.
        let seats = json!([
            sw("work", true, Some(0), Some(80)),
            sw("week", false, Some(80), Some(0))
        ]);
        let desk = judged_by(served(json!([accounts[0], accounts[2]])), seats);
        let summary = pool("claude", &desk).unwrap();
        assert_eq!(
            (
                &summary["with_room"],
                &summary["used_up"],
                &summary["regains_room_at"],
                &summary["next_alias"]
            ),
            (
                &json!(0),
                &json!(2),
                &json!(moment(NOW + 3 * HOUR)),
                &Value::Null
            )
        );
        // A seat that cannot be read: it is not known that every seat is used up.
        let seats = json!([
            sw("work", true, Some(0), Some(80)),
            sw("out", false, None, None)
        ]);
        let desk = judged_by(served(json!([accounts[0], accounts[3]])), seats);
        assert_eq!(
            pool("claude", &desk).unwrap()["regains_room_at"],
            Value::Null
        );
        // No pool read for the tool: no summary.
        assert!(pool("codex", &served(accounts.clone())).is_none());
        assert!(pool("claude", &served(json!([]))).is_none());
    }

    #[test]
    fn a_hard_limit_is_told_only_where_no_seat_of_the_pool_would_take_the_turn() {
        let hard = json!({ "type": "provider-limit", "kind": "hard_limit", "provider_id": "codex",
            "resets_at": moment(NOW + 3 * HOUR) });
        assert!(limit_event(hard.clone(), Failover::Spare, None).is_none());
        let told = limit_event(hard.clone(), Failover::None, Some(NOW + HOUR)).unwrap();
        assert_eq!(
            (
                &told["kind"],
                &told["failover"],
                &told["pool_regains_at"],
                &told["resets_at"]
            ),
            (
                &json!("hard_limit"),
                &json!("none"),
                &json!(moment(NOW + HOUR)),
                &hard["resets_at"]
            )
        );
        let told = limit_event(hard, Failover::Unknown, None).unwrap();
        assert_eq!(
            (&told["failover"], &told["pool_regains_at"]),
            (&json!("unknown"), &Value::Null)
        );
        // A warning, a recovery: as they are, also while a seat has room.
        for kind in ["warning", "recovered", "unknown"] {
            let event = json!({ "type": "provider-limit", "kind": kind });
            assert_eq!(
                limit_event(event.clone(), Failover::Spare, None),
                Some(event)
            );
        }
        // A reading holds until it grows old: ten minutes at least after it was read.
        let desk = served(json!([timed(
            "work",
            true,
            "ok",
            &[("5h", 0.1, NOW + HOUR)]
        )]));
        assert_eq!(fresh_until("claude", &desk), Some(NOW - 60_000 + 600_000));
        assert_eq!(fresh_until("codex", &desk), None);
    }

    #[test]
    fn the_seat_in_use_is_named_by_its_tag() {
        let mut a = sw("a", false, Some(0), Some(50));
        a["tag"] = json!("tag-a");
        let mut b = sw("b", true, Some(40), Some(50));
        b["tag"] = json!("tag-b");
        let desk = judged_by(served(json!([])), json!([a, b]));
        assert_eq!(seat_in_use("claude", &desk).as_deref(), Some("tag-b"));
        assert_eq!(seat_in_use("codex", &desk), None);
    }

    #[test]
    fn the_host_reads_the_seats_first_where_it_switches_them_and_its_reading_is_old() {
        let fresh = served(json!([timed(
            "work",
            true,
            "ok",
            &[("5h", 0.85, NOW + HOUR)]
        )]));
        let mut old = fresh.clone();
        old["providers"][0]["stale"] = json!(true);
        assert!(!needs_reading(Accounts::Select, "claude", true, &fresh));
        assert!(needs_reading(Accounts::Select, "claude", true, &old));
        assert!(needs_reading(Accounts::Select, "claude", true, &json!({})));
        // Only where this Host switches the seats: by the selector, or by the automatic lane.
        assert!(needs_reading(Accounts::Select, "claude", false, &old));
        let mut no_lane = old.clone();
        no_lane["switcher"]["enabled"] = json!(false);
        assert!(!needs_reading(Accounts::Select, "claude", false, &no_lane));
        assert!(!needs_reading(Accounts::Follow, "claude", true, &old));
        assert!(!needs_reading(Accounts::None, "claude", true, &old));
    }

    #[test]
    fn a_warning_is_told_only_where_no_seat_would_take_the_turn() {
        let warning =
            json!({ "type": "provider-limit", "kind": "warning", "provider_id": "codex" });
        assert!(warning_event(warning.clone(), Failover::Spare).is_none());
        assert!(warning_event(warning.clone(), Failover::Unknown).is_none());
        let told = warning_event(warning, Failover::None).unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"]),
            (&json!("warning"), &json!("none"))
        );
    }

    #[test]
    fn a_tool_with_no_pool_this_host_switches_is_one_account_and_its_whole_pool() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        let desk = desk(true, "automatic", read(json!([in_use])));
        // Antigravity: no selector and no lane, so no pool: none would take the turn, and the
        // near row is said.
        let of = |provider: &str, selector: bool, desk: &Value| {
            failover(Accounts::Select, provider, "", selector, desk)
        };
        assert_eq!(of("agy", false, &desk), Failover::None);
        assert!(!needs_reading(Accounts::Select, "agy", false, &desk));
        // A tool with the owner's selector that the seat tool does not list, while it lists
        // Claude: one account, its whole pool, and nothing to read again for it.
        let mut listed = desk.clone();
        listed["providers"].as_array_mut().unwrap().push(
            json!({ "provider": "codex", "read_state": "never", "stale": true, "accounts": [] }),
        );
        assert_eq!(of("codex", true, &listed), Failover::None);
        assert!(!needs_reading(Accounts::Select, "codex", true, &listed));
        // Where nothing is fresh at all, it cannot be known: no near, and the seats are read.
        let mut none_fresh = listed.clone();
        none_fresh["providers"][0]["stale"] = json!(true);
        assert_eq!(of("codex", true, &none_fresh), Failover::Unknown);
        assert!(needs_reading(Accounts::Select, "codex", true, &none_fresh));
    }
}
