//! The one line above the composer for the messages waiting behind the
//! running turn: "1 waiting · Steer now". Its action reads them into the
//! running turn, or stops the turn for them where it cannot take them, or,
//! after a Stop, sends them. Every word the desktop shows for it lives here
//! (the iOS and Android apps use the same words), and the local switch that
//! hides it.

/// This device hides the line (Settings and Ctrl+K: Show queue).
pub const SHOWN_KEY: &str = "conversation/queueShown";

/// How often the open chat's queue is read again from the Host while
/// something waits, so an entry the Host no longer has never lingers.
pub const REREAD_SECS: u64 = 30;

/// The line's count: "1 waiting", "3 waiting".
pub fn waiting(count: i32) -> String {
    format!("{} waiting", count.max(0))
}

/// The line's action, by what it does now.
pub const STEER_NOW: &str = "Steer now";
pub const STOP_AND_SEND: &str = "Stop and send";
pub const SEND_NOW: &str = "Send now";

/// Its tip.
pub fn action_tip(kind: &str) -> &'static str {
    match kind {
        "steer" => "Read into this turn",
        "take-over" => "Stop this turn and send",
        _ => "Send now",
    }
}

/// What a Send now could not do: neither steer, stop the turn nor be sent.
/// The message waits on. Calm, and only as a last resort.
pub const STILL_WAITING: &str = "Still waiting: this turn can't take it or be stopped now";

/// The line under it for a refused Send now: this app's own words as they
/// are, an older Host's (which steers nothing) after "Not sent now".
pub fn refusal_line(words: &str) -> String {
    match words.trim() {
        "" => String::new(),
        STILL_WAITING => STILL_WAITING.to_owned(),
        words => format!("Not sent now: {words}"),
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
