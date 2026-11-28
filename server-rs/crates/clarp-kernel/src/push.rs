//! What a phone is sent: the payload of a push.
//!
//! A push is sent for two things. A turn ended with something for the user: a banner like a
//! chat message, quiet unless the user still has something to answer. And an agent asks the
//! user something: the one thing it cannot finish without the user, so it is told when it is
//! asked, not when the turn ends.
//!
//! The apps read these payloads (the notification extension that shows the agent's portrait,
//! the link a tap follows, the reply typed from the banner), so they are the Python Host's,
//! field for field (`server/lib/apns.py`: `turn_done_payload`, `decision_payload`).

use serde_json::{Value, json};

use crate::notify_events::{Event, Loc};

/// What a banner says when there is nothing of the reply to show.
const DEFAULT_BODY: &str = "Done — your turn 👋";
/// Its key ([`crate::notify_events::STRINGS`]).
const DEFAULT_BODY_KEY: &str = "notify.body.done";

/// The first `max` characters.
fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// A finished turn, as a phone is told of it. An empty text is "not there".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Told<'a> {
    pub persona: &'a str,
    /// The banner's title where it is not the agent's name. Empty: the name.
    pub title: &'a str,
    pub session: &'a str,
    /// What the agent said, as a preview.
    pub body: &'a str,
    /// Where the agent's portrait is, for the device that is told.
    pub avatar_url: &'a str,
    /// The portrait is one the user gave the agent.
    pub avatar_custom: bool,
    pub notification_id: &'a str,
    /// Which Host this is, for an app that knows several.
    pub server_instance_id: &'a str,
    /// The user has something to answer: the push makes a sound.
    pub needs_response: bool,
    /// The notification event's wire id ([`crate::notify_events`]). Empty: none is said.
    pub event: &'a str,
    /// The row a tap opens: the reply's or the note's.
    pub message_id: &'a str,
    pub agent_id: &'a str,
    /// The actions the app offers on it (`aps.category`). Empty: none.
    pub category: &'a str,
    /// Where the Host made the title or the body itself (never the agent's words): the key
    /// the apps translate it by.
    pub title_loc: Option<&'a Loc>,
    pub body_loc: Option<&'a Loc>,
}

/// Puts a Host-made text's key and arguments on a payload, at its top level, where the app's
/// own extension and an Android phone translate it: `title_loc_key` and `title_loc_args`, or
/// `body_loc_key` and `body_loc_args`. The English text stays in `aps.alert`, which is what
/// an app without the key shows (an `aps.alert` `loc-key` would replace it with the key on
/// such an app).
pub fn put_loc(payload: &mut Value, part: &str, loc: &Loc) {
    payload[format!("{part}_loc_key")] = json!(loc.key);
    payload[format!("{part}_loc_args")] = json!(loc.args);
}

/// Says the event a payload is of, and how relevant the lock screen takes it to be.
fn put_event(payload: &mut Value, event: Event) {
    payload["event"] = json!(event.id());
    payload["aps"]["relevance-score"] = json!(event.spec().relevance);
}

/// The payload for a turn that ended.
pub fn turn_done(told: &Told<'_>) -> Value {
    let name = match told.persona.trim() {
        "" => "Clarp",
        name => name,
    };
    let default_body = Loc::new(DEFAULT_BODY_KEY, &[]);
    // The key goes with an event: a push of no event is the one old apps were always sent.
    let (body, body_loc) = if told.body.is_empty() {
        let keyed = told
            .body_loc
            .or((!told.event.is_empty()).then_some(&default_body));
        (DEFAULT_BODY, keyed)
    } else {
        (told.body, told.body_loc)
    };
    let title = if told.title.is_empty() {
        name
    } else {
        told.title
    };
    let mut payload = json!({
        "aps": {
            "alert": { "title": title, "body": body },
            // A hint to refresh the conversation before the app is opened again.
            "content-available": 1,
            // The level of a chat message: it respects Focus and Do Not Disturb.
            "interruption-level": "active",
            // Lets the app's extension show the agent's portrait.
            "mutable-content": 1,
        },
        "kind": "user-notification",
        "session": told.session,
        "persona": name,
        "needs_response": told.needs_response,
    });
    if told.needs_response {
        payload["aps"]["sound"] = json!("default");
    }
    if !told.notification_id.is_empty() {
        payload["notification_id"] = json!(told.notification_id);
    }
    if !told.server_instance_id.is_empty() {
        payload["server_instance_id"] = json!(told.server_instance_id);
    }
    if !told.session.is_empty() {
        payload["aps"]["thread-id"] = json!(told.session);
    }
    if !told.avatar_url.is_empty() {
        payload["avatar_url"] = json!(told.avatar_url);
        if told.avatar_custom {
            payload["avatar_custom"] = json!(true);
        }
    }
    if let Some(event) = Event::named(told.event) {
        put_event(&mut payload, event);
    }
    for (key, value) in [("message_id", told.message_id), ("agent_id", told.agent_id)] {
        if !value.is_empty() {
            payload[key] = json!(value);
        }
    }
    if !told.category.is_empty() {
        payload["aps"]["category"] = json!(told.category);
    }
    if let Some(loc) = told.title_loc {
        put_loc(&mut payload, "title", loc);
    }
    if let Some(loc) = body_loc {
        put_loc(&mut payload, "body", loc);
    }
    payload
}

/// The silent push that tells every phone a chat was read elsewhere (opened on a device,
/// marked read, or its request answered): the phone takes that chat's notifications down.
/// `decision_id`: the request that was answered, dismissed or withdrawn, where one was.
pub fn read(session: &str, server_id: &str, decision_id: &str) -> Value {
    let mut payload = json!({
        "aps": { "content-available": 1 },
        "kind": "read",
        "session": session,
        "server_instance_id": server_id,
    });
    if !decision_id.is_empty() {
        payload["decision_id"] = json!(decision_id);
    }
    payload
}

/// The name a read push goes by: a chat read takes the place of the chat's last one, and the
/// end of a request that of the same request's, never another's.
pub fn read_collapse_id(session: &str, decision_id: &str) -> String {
    if decision_id.is_empty() {
        clip(&format!("read-{session}"), 64)
    } else {
        clip(&format!("read-decision-{decision_id}"), 64)
    }
}

/// A silent wake that lets an app recompute its icon badge from device-local unread state.
pub fn badge_wake(server_id: &str) -> Value {
    json!({
        "aps": { "content-available": 1 },
        "kind": "sync",
        "server_instance_id": server_id,
    })
}

/// A request to the user, as a phone is told of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Asked<'a> {
    pub persona: &'a str,
    pub session: &'a str,
    pub title: &'a str,
    pub question: &'a str,
    pub decision_id: &'a str,
    pub artifact_id: &'a str,
    /// The agent is blocked on it and said it cannot wait: it breaks through Focus.
    pub time_sensitive: bool,
    /// The answer is a text, which can be typed from the banner.
    pub text_input: bool,
    pub revision: i64,
    pub expires_at: Option<i64>,
    pub avatar_url: &'a str,
    pub avatar_custom: bool,
    pub server_instance_id: &'a str,
    pub agent_id: &'a str,
    /// `approval`, `single_choice` or `text_input`. Empty: not said.
    pub response_type: &'a str,
    /// The choices of a `single_choice` (offered as buttons where there are two to four), or
    /// the yes and no of an `approval` (`accepted`, `rejected`), `{id, label}` each. None where
    /// what it asks is withheld (an agent whose words go to no push service).
    pub options: &'a [Value],
    /// Where the Host made the title or the question itself.
    pub title_loc: Option<&'a Loc>,
    pub body_loc: Option<&'a Loc>,
}

/// The most choices a notification offers as buttons.
pub const MOST_OPTIONS: usize = 4;

/// The actions a request's notification offers: yes and no, its choices, or a typed answer.
/// Empty where it offers none (a choice of more than four: the app is opened).
pub fn decision_category(response_type: &str, options: usize, text_input: bool) -> String {
    if text_input || response_type == "text_input" {
        return "input-request".to_string();
    }
    match response_type {
        "approval" => "decision-approval".to_string(),
        "single_choice" if (2..=MOST_OPTIONS).contains(&options) => {
            format!("decision-choice-{options}")
        }
        _ => String::new(),
    }
}

/// The answers a request's notification offers as buttons, `{id, label}` each: an approval's
/// yes and no, or a choice of two to four. `None` where its category offers none.
pub fn offered_options(category: &str, options: &[Value]) -> Option<Vec<Value>> {
    if !(category.starts_with("decision-choice-") || category == "decision-approval") {
        return None;
    }
    Some(
        options
            .iter()
            .map(|option| json!({ "id": option["id"], "label": option["label"] }))
            .collect(),
    )
}

/// The name a request's push goes by: sent again, it takes the place of the first.
pub fn decision_notification_id(decision_id: &str) -> String {
    clip(&format!("decision-{decision_id}"), 64)
}

/// The payload for a request that was just made. A tap leads to the agent's conversation,
/// where the request is pinned.
pub fn decision(asked: &Asked<'_>) -> Value {
    let said = if asked.question.is_empty() {
        asked.title
    } else {
        asked.question
    };
    let body = clip(said.trim(), 500);
    let notification_id = decision_notification_id(asked.decision_id);
    let mut payload = turn_done(&Told {
        persona: asked.persona,
        session: asked.session,
        body: &body,
        avatar_url: asked.avatar_url,
        avatar_custom: asked.avatar_custom,
        notification_id: &notification_id,
        server_instance_id: asked.server_instance_id,
        // A request is something to answer: it always makes a sound.
        needs_response: true,
        event: Event::NeedsYou.id(),
        agent_id: asked.agent_id,
        title_loc: asked.title_loc,
        body_loc: asked.body_loc,
        ..Told::default()
    });
    let heading = asked.title.trim();
    if !heading.is_empty() && payload["aps"]["alert"]["body"] != heading {
        payload["aps"]["alert"]["subtitle"] = json!(clip(heading, 120));
    }
    payload["aps"]["interruption-level"] = json!(if asked.time_sensitive {
        "time-sensitive"
    } else {
        "active"
    });
    payload["reason"] = json!("decision");
    let category = decision_category(asked.response_type, asked.options.len(), asked.text_input);
    if !category.is_empty() {
        payload["aps"]["category"] = json!(category);
    }
    if !asked.response_type.is_empty() {
        payload["response_type"] = json!(asked.response_type);
    }
    if let Some(offered) = offered_options(&category, asked.options) {
        payload["options"] = json!(offered);
    }

    // The request's revision, on every request: an answer from a button on the notification
    // resolves it without reading the card first.
    payload["revision"] = json!(asked.revision);
    if asked.text_input {
        payload["response_type"] = json!("text_input");
        if let Some(expires_at) = asked.expires_at {
            payload["expires_at"] = json!(expires_at);
        }
    }
    payload["decision_id"] = json!(asked.decision_id);
    payload["artifact_id"] = json!(asked.artifact_id);
    payload
}

/// A ring, as a phone is told of it: an agent calls, or its call ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Rung<'a> {
    pub ring_id: &'a str,
    /// The ring's state now.
    pub state: &'a str,
    pub session: &'a str,
    pub persona: &'a str,
    pub reason: &'a str,
    pub opening_line: &'a str,
    pub opening_audio_url: &'a str,
    pub expires_at: i64,
    /// What happened: `ring` for the incoming call, then `answered`, `declined`, `missed`
    /// or `cancelled`.
    pub kind: &'a str,
    /// The owner lets a locked phone show why the agent calls.
    pub show_reason: bool,
    pub avatar_url: &'a str,
    pub avatar_custom: bool,
    pub server_instance_id: &'a str,
}

/// The name every push of one ring goes by: the incoming call is replaced by what became
/// of it.
pub fn ring_notification_id(ring_id: &str) -> String {
    clip(&format!("ring-{ring_id}"), 64)
}

/// The payload for a ring. The incoming call breaks through Focus, makes a sound and has
/// the actions the app registered for a call (answer, decline); what became of it is quiet,
/// except that a missed call is shown like a message.
pub fn ring(rung: &Rung<'_>) -> Value {
    let persona = match rung.persona.trim() {
        "" => "Clarp",
        name => name,
    };
    let title_loc = Loc::new(
        match rung.kind {
            "missed" => "notify.body.call_missed",
            "declined" => "notify.body.call_declined",
            "answered" => "notify.body.call_answered",
            "cancelled" => "notify.body.call_cancelled",
            _ => "notify.body.call_ringing",
        },
        &[persona],
    );
    let title = title_loc.english();
    // Who is calling is shown on a locked phone; why, only when the owner said so.
    let incoming = Loc::new("notify.body.call_incoming", &[]);
    let body = if rung.show_reason {
        clip(rung.reason, 300)
    } else {
        incoming.english()
    };
    let notification_id = ring_notification_id(rung.ring_id);
    let mut payload = json!({
        "aps": {
            "alert": { "title": title, "body": body },
            "mutable-content": 1,
            "thread-id": format!("ring-{}", rung.session),
        },
        "kind": "agent-ring",
        "ring_id": rung.ring_id,
        "ring_state": rung.state,
        "session": rung.session,
        "persona": persona,
        "opening_line": rung.opening_line,
        "opening_audio_url": rung.opening_audio_url,
        "expires_at": rung.expires_at,
        "notification_id": notification_id,
    });
    // Why the agent calls is not in what a phone is pushed, unless the owner lets a locked
    // phone show it: a ring opened from the notification then has no line of why.
    if rung.show_reason {
        payload["reason"] = json!(rung.reason);
    }
    put_event(&mut payload, Event::Call);
    put_loc(&mut payload, "title", &title_loc);
    if !rung.show_reason {
        put_loc(&mut payload, "body", &incoming);
    }
    if rung.kind == "ring" {
        payload["aps"]["interruption-level"] = json!("time-sensitive");
        payload["aps"]["sound"] = json!("default");
        payload["aps"]["category"] = json!("agent-call");
        payload["aps"]["relevance-score"] = json!(1.0);
    } else {
        payload["aps"]["interruption-level"] = json!(if rung.kind == "missed" {
            "active"
        } else {
            "passive"
        });
    }
    if !rung.server_instance_id.is_empty() {
        payload["server_instance_id"] = json!(rung.server_instance_id);
    }
    if !rung.avatar_url.is_empty() {
        payload["avatar_url"] = json!(rung.avatar_url);
        if rung.avatar_custom {
            payload["avatar_custom"] = json!(true);
        }
    }
    payload
}

/// Whether a request breaks through Focus: only when the agent is blocked on it and has
/// said that it cannot wait. Blocked alone, or urgent alone, is an ordinary notification.
pub fn needs_interruption(blocks_progress: bool, urgency: &str) -> bool {
    blocks_progress && urgency == "time_sensitive"
}

/// The id under which a later push replaces an earlier one. Every finished turn has its
/// own, so that replies that come close together do not replace each other; only the same
/// turn, sent again, collapses.
pub fn collapse_id(payload: &Value) -> String {
    let text = |key: &str| payload[key].as_str().unwrap_or("").trim();
    let notification_id = text("notification_id");
    if !notification_id.is_empty() {
        return clip(notification_id, 64);
    }
    match text("session") {
        "" => "turn-done".to_string(),
        session => clip(&format!("turn-done-{session}"), 64),
    }
}

/// A ring pushed to a phone as an incoming call (a push of the kind the phone must show as a
/// call the moment it arrives): no alert, only what the phone needs to say who calls. `ring`
/// is the ring as it is kept.
pub fn call(rung: &Rung<'_>, conversation_mode: &str) -> Value {
    let persona = match rung.persona.trim() {
        "" => "Clarp",
        persona => persona,
    };
    let mut payload = json!({
        "kind": "agent-ring", "ring_id": rung.ring_id, "ring_state": "ringing",
        "session": rung.session, "persona": persona, "expires_at": rung.expires_at,
    });
    // Why the agent calls, only where the owner lets a locked phone show it.
    if rung.show_reason {
        payload["reason"] = json!(rung.reason);
    }
    // A ring held as a live voice call says so; an ordinary one says nothing of it.
    if !conversation_mode.is_empty() {
        payload["conversation_mode"] = json!(conversation_mode);
    }
    if !rung.server_instance_id.is_empty() {
        payload["server_instance_id"] = json!(rung.server_instance_id);
    }
    payload
}

/// Until when the push service keeps a call push for a phone it cannot reach: the second
/// the ring is missed in.
pub fn call_expires(expires_at_ms: i64) -> i64 {
    expires_at_ms.div_euclid(1000)
}

/// What became of the call pushes of one ring, as the one who rang is told. `asked`: the
/// ring was for a phone that left a token for calls, and the Host has a key of its own at
/// the service; where it is not so the Host pushes no calls, and says so. `answers`: the
/// status the push service gave for each phone that was pushed, and `None` where it could
/// not be asked. A ring can be asked for and reach nobody: where every phone that left a
/// token is no longer paired, none is pushed.
pub fn calls_counted(asked: bool, answers: &[Option<u16>]) -> Value {
    let sent = answers
        .iter()
        .filter(|answer| **answer == Some(200))
        .count();
    json!({ "enabled": asked, "sent": sent, "failed": answers.len() - sent })
}

/// A collapse id as the push service takes it: at most 64 bytes, cut between characters.
pub fn collapse_id_on_the_wire(collapse_id: &str) -> &str {
    let mut end = collapse_id.len().min(64);
    while !collapse_id.is_char_boundary(end) {
        end -= 1;
    }
    &collapse_id[..end]
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn an_ordinary_reply_is_quiet_and_one_that_leaves_a_question_makes_a_sound() {
        let told = Told {
            persona: "Vera",
            session: "vera",
            body: "The build is green.",
            notification_id: "pn-1",
            ..Told::default()
        };
        let quiet = turn_done(&told);
        assert_eq!(quiet["aps"]["alert"]["body"], "The build is green.");
        assert_eq!(quiet["aps"]["thread-id"], "vera");
        assert!(quiet["aps"].get("sound").is_none());
        let loud = turn_done(&Told {
            needs_response: true,
            ..told
        });
        assert_eq!(loud["aps"]["sound"], "default");
        assert_eq!(loud["aps"]["interruption-level"], "active");
        assert_eq!(collapse_id(&loud), "pn-1");
    }

    #[test]
    fn a_collapse_id_is_cut_at_64_bytes_between_characters() {
        let long = "é".repeat(40);
        assert_eq!(collapse_id_on_the_wire(&long).len(), 64);
        let odd = format!("a{}", "é".repeat(40));
        assert_eq!(collapse_id_on_the_wire(&odd).len(), 63);
        assert_eq!(collapse_id_on_the_wire("short"), "short");
    }

    #[test]
    fn a_reply_says_its_event_row_and_agent_and_keys_only_host_made_text() {
        let told = Told {
            persona: "Vera",
            session: "vera",
            body: "The build is green.",
            notification_id: "pn-1",
            event: "reply",
            message_id: "m-9",
            agent_id: "a1",
            category: "agent-turn",
            ..Told::default()
        };
        let payload = turn_done(&told);
        assert_eq!(payload["event"], "reply");
        assert_eq!(payload["message_id"], "m-9");
        assert_eq!(payload["agent_id"], "a1");
        assert_eq!(payload["aps"]["category"], "agent-turn");
        assert_eq!(payload["aps"]["relevance-score"], 0.5);
        // The agent's own words carry no key; nothing goes into `aps.alert` but the text.
        assert!(payload.get("body_loc_key").is_none());
        assert!(payload["aps"]["alert"].get("loc-key").is_none());
        let empty = turn_done(&Told { body: "", ..told });
        assert_eq!(empty["aps"]["alert"]["body"], DEFAULT_BODY);
        assert_eq!(empty["body_loc_key"], "notify.body.done");
        assert_eq!(empty["body_loc_args"], json!([]));
        // An old payload stays as it was: no event, no new fields.
        let old = turn_done(&Told {
            persona: "Vera",
            session: "vera",
            body: "Hi",
            ..Told::default()
        });
        for key in ["event", "message_id", "agent_id"] {
            assert!(old.get(key).is_none(), "{key}");
        }
        assert!(old["aps"].get("category").is_none());
    }

    #[test]
    fn a_request_offers_its_choices_as_buttons_up_to_four() {
        let options = [
            json!({"id": "a", "label": "Ship", "description": "now"}),
            json!({"id": "b", "label": "Wait"}),
            json!({"id": "c", "label": "Drop"}),
        ];
        let asked = Asked {
            persona: "Vera",
            session: "vera",
            title: "Release?",
            question: "Which way?",
            decision_id: "d1",
            artifact_id: "art1",
            response_type: "single_choice",
            options: &options,
            agent_id: "a1",
            ..Asked::default()
        };
        let payload = decision(&asked);
        assert_eq!(payload["event"], "needs_you");
        assert_eq!(payload["aps"]["category"], "decision-choice-3");
        assert_eq!(payload["aps"]["relevance-score"], 1.0);
        assert_eq!(payload["aps"]["sound"], "default");
        assert_eq!(payload["response_type"], "single_choice");
        assert_eq!(
            payload["options"],
            json!([{"id": "a", "label": "Ship"}, {"id": "b", "label": "Wait"}, {"id": "c", "label": "Drop"}])
        );
        assert_eq!(payload["notification_id"], "decision-d1");
        assert_eq!(payload["revision"], 0);
        let five = [options.to_vec(), options[..2].to_vec()].concat();
        let many = decision(&Asked {
            options: &five,
            ..asked
        });
        assert!(many["aps"].get("category").is_none());
        assert!(many.get("options").is_none());
        let yes_no = [
            json!({"id": "accepted", "label": "Ship it"}),
            json!({"id": "rejected", "label": "No"}),
        ];
        let approval = decision(&Asked {
            response_type: "approval",
            options: &yes_no,
            ..asked
        });
        assert_eq!(approval["aps"]["category"], "decision-approval");
        assert_eq!(approval["options"], json!(yes_no));
        let typed = decision(&Asked {
            response_type: "text_input",
            text_input: true,
            options: &[],
            ..asked
        });
        assert_eq!(typed["aps"]["category"], "input-request");
        // What it asks is withheld (a bound agent): nothing answers it from the banner.
        let withheld = decision(&Asked {
            response_type: "",
            options: &[],
            ..asked
        });
        assert!(withheld["aps"].get("category").is_none());
        assert!(withheld.get("options").is_none());
        assert!(withheld.get("response_type").is_none());
        assert_eq!(withheld["decision_id"], "d1");
        assert_eq!(typed["response_type"], "text_input");
    }

    #[test]
    fn a_ring_is_the_call_event_with_keyed_titles() {
        let rung = Rung {
            ring_id: "r1",
            session: "vera",
            persona: "Vera",
            kind: "missed",
            ..Rung::default()
        };
        let payload = ring(&rung);
        assert_eq!(payload["event"], "call");
        assert_eq!(payload["aps"]["alert"]["title"], "Missed call from Vera");
        assert_eq!(payload["title_loc_key"], "notify.body.call_missed");
        assert_eq!(payload["title_loc_args"], json!(["Vera"]));
        assert_eq!(payload["body_loc_key"], "notify.body.call_incoming");
        let shown = ring(&Rung {
            show_reason: true,
            reason: "The deploy is down",
            ..rung
        });
        assert!(shown.get("body_loc_key").is_none());
    }

    #[test]
    fn a_read_push_is_silent_and_names_the_chat() {
        let payload = read("vera", "host-1", "d1");
        assert_eq!(
            payload,
            json!({"aps": {"content-available": 1}, "kind": "read", "session": "vera",
                   "server_instance_id": "host-1", "decision_id": "d1"})
        );
        assert!(read("vera", "host-1", "").get("decision_id").is_none());
        assert_eq!(read_collapse_id("vera", ""), "read-vera");
        assert_eq!(read_collapse_id("vera", "d1"), "read-decision-d1");
    }
}
