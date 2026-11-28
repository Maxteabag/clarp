//! The one line above the composer for the messages waiting behind the
//! running turn: "1 waiting · Steer now". Its action reads them into the
//! running turn, or stops the turn for them where it cannot take them, or,
//! after a Stop, sends them. Every word the desktop shows for it lives here
//! (the iOS and Android apps use the same words).

use crate::tr;

// i18n-screen: chat

/// How often the open chat's queue is read again from the Host while
/// something waits, so an entry the Host no longer has never lingers.
pub const REREAD_SECS: u64 = 30;

/// ID of an owner's queued message that has not started. A message left
/// for later can take over a turn, although it cannot be steered into one.
pub fn waiting_owner_id(item: &serde_json::Value) -> Option<&str> {
    let owner = item["origin"].as_str().is_none_or(|origin| matches!(origin, "user" | "oracle"));
    if !owner || !item["sender"].is_null() || matches!(item["status"].as_str(), Some("started" | "claimed")) {
        return None;
    }
    item["queue_id"].as_str().filter(|id| !id.is_empty())
        .or_else(|| item["id"].as_str().filter(|id| !id.is_empty()))
}

/// The line's count: "1 waiting", "3 waiting".
pub fn waiting(count: i32) -> String {
    // i18n: chat, line above the composer: how many of the user's messages wait behind the running turn; {n} is the count
    tr!("{n} waiting" | "{n} waiting" % count.max(0))
}

/// The line's action, by what it does now.
pub fn steer_now() -> String {
    // i18n: chat, button on the waiting-messages line: read the waiting message into the running turn now (imperative)
    tr!("Steer now")
}

pub fn stop_and_send() -> String {
    // i18n: chat, button on the waiting-messages line: stop the running turn, then send (imperative)
    tr!("Stop and send")
}

pub fn send_now() -> String {
    // i18n: chat, button on the waiting-messages line and its tooltip: send the waiting message now (imperative)
    tr!("Send now")
}

/// Its tip.
pub fn action_tip(kind: &str) -> String {
    match kind {
        // i18n: chat, tooltip of Steer now on the waiting-messages line
        "steer" => tr!("Read into this turn"),
        // i18n: chat, tooltip of Stop and send on the waiting-messages line
        "take-over" => tr!("Stop this turn and send"),
        _ => send_now(),
    }
}

/// What a Send now could not do: neither steer, stop the turn nor be sent.
/// The message waits on. Calm, and only as a last resort.
/// Kept as this English sentence in the engine's state; `refusal_line`
/// draws it in the app's language.
pub const STILL_WAITING: &str = "Still waiting: this turn can't take it or be stopped now";

/// The line under it for a refused Send now: this app's own words as they
/// are, an older Host's (which steers nothing) after "Not sent now".
pub fn refusal_line(words: &str) -> String {
    match words.trim() {
        "" => String::new(),
        // i18n: chat, line under the waiting messages after Send now could neither steer, stop the turn nor send
        STILL_WAITING => tr!("Still waiting: this turn can't take it or be stopped now"),
        // i18n: chat, line under the waiting messages after the Host refused Send now; {} is the Host's own reason
        words => tr!("Not sent now: {}", words),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_line_says_what_waits_and_why_it_was_not_sent() {
        assert_eq!(waiting(1), "1 waiting");
        assert_eq!(waiting(3), "3 waiting");
        assert_eq!(refusal_line(STILL_WAITING), STILL_WAITING);
        assert_eq!(refusal_line("agent is still working"), "Not sent now: agent is still working");
        assert_eq!(refusal_line("  "), "");
        assert_eq!(action_tip("steer"), "Read into this turn");
    }
}
