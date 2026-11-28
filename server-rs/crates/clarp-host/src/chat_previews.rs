//! The chat list's previews on an event stream (feature `events_chat_previews`, issues 905 and
//! 913).
//!
//! An app that shows a list of a hundred chats and one or two of them open was sent every
//! chat's every change: each `transcript-updated` (and its `/log` read), each tool's
//! `agent-activity`, each state, each status line of each turn. Of a chat whose pane is not on
//! screen the list shows one line: its newest message and what it does. A stream opened with
//! `chat_previews=1` names the chats on screen (`visible`, beside `live`); a chat both followed
//! and on screen is sent all of it, as before. Of every other chat the stream sends, in place of
//! those changes, a preview: an `agent-roster` event of kind `preview` with the fields of the
//! chat's snapshot row the list shows ([`crate::edge::PREVIEW_FIELDS`]), at most one every
//! [`EVERY`] per chat, made when it is sent from what the Host holds of the agent then.
//!
//! What the app acts on goes on as before for every chat: the roster's own events (with
//! availability), notifications, decisions, queue and delivery, a send's receipt (an
//! `agent-state` that names the message it started with) and a turn's `turn` ops. A preview
//! has no number: it moves no cursor, and a replay never holds one. A stream's opening and a
//! catch-up end with the plain `agent-roster`, on which the app reads the snapshot: of what it
//! leaves out of a replay it sends nothing.
//!
//! Who may read what is decided as before: a preview names its agent by `session` and
//! `agent_id`, so the guard of bound privacy blanks it for a reader who may not read the agent
//! as it blanks that agent's row in the snapshot, and its message is the one the row shows
//! (hidden coordination included).

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use serde::Deserialize;
use serde_json::{Value, json};
use tokio::time::Instant;

use crate::fleet::Directory;

/// At most one preview of a chat this often.
pub const EVERY: Duration = Duration::from_secs(5);
/// How long a chat that changed after a quiet while waits for the changes that come with it
/// (the end of a turn is its row, its state and its activity, a moment apart).
pub const SETTLE: Duration = Duration::from_millis(250);

/// The fields of a preview that carry the chat's newest message: sent again only when one of
/// them moved. The rest, small, go in every preview.
const MESSAGE_FIELDS: [&str; 5] = [
    "conversation_id",
    "head_revision",
    "last_message_id",
    "last_message",
    "last_message_preview",
];

/// What a stream does with an event about one chat that is not shown whole.
#[derive(Debug, PartialEq, Eq)]
pub enum Fate {
    /// Sent as it is.
    Send,
    /// Sent as it is, and the chat's preview is due.
    SendAndPreview(Chat),
    /// Not sent: the chat's preview stands for it.
    Preview(Chat),
}

/// A chat, as an event names it.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Chat {
    pub session: String,
    pub agent_id: String,
}

impl Chat {
    /// The name a stream keeps it under: its id, which a rename does not change.
    fn key(&self) -> &str {
        if self.agent_id.is_empty() {
            &self.session
        } else {
            &self.agent_id
        }
    }
}

/// The fields of an event a stream judges it by.
#[derive(Deserialize)]
struct About {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    session: Option<String>,
    #[serde(default)]
    agent_id: Option<String>,
    /// Present on a dispatch's `agent-state`: the receipt of the message the turn started with.
    #[serde(default)]
    client_msg_id: Option<Value>,
}

/// The kinds of event a preview stands for, of a chat not shown whole.
const FOLDED: [&str; 3] = [
    "\"transcript-updated\"",
    "\"agent-activity\"",
    "\"agent-state\"",
];

/// What becomes of a numbered event on a stream that shows previews, by whose it is: `full`
/// says whether a chat is shown whole.
pub fn fate(json: &str, full: impl Fn(&Chat) -> bool) -> Fate {
    // Read off the event as it is written, so that the many of other kinds cost nothing.
    if !FOLDED.iter().any(|kind| json.contains(kind)) {
        return Fate::Send;
    }
    let Ok(about) = serde_json::from_str::<About>(json) else {
        return Fate::Send;
    };
    if !matches!(
        about.kind.as_str(),
        "transcript-updated" | "agent-activity" | "agent-state"
    ) {
        return Fate::Send;
    }
    let chat = Chat {
        session: about.session.unwrap_or_default(),
        agent_id: about.agent_id.unwrap_or_default(),
    };
    if chat.session.is_empty() && chat.agent_id.is_empty() || full(&chat) {
        return Fate::Send;
    }
    // A send's receipt: the app acts on it wherever the chat is.
    if about.kind == "agent-state" && about.client_msg_id.is_some() {
        return Fate::SendAndPreview(chat);
    }
    Fate::Preview(chat)
}

/// Whether a chat is among `names` (sessions or agent ids, `*` for every one).
pub fn named(names: &HashSet<String>, session: &str, agent_id: &str) -> bool {
    let has = |name: &str| !name.is_empty() && names.contains(name);
    names.contains("*") || has(session) || has(agent_id)
}

struct Sent {
    chat: Chat,
    at: Instant,
    fields: Value,
}

/// A stream's previews: the chats on screen, the previews due and what each chat's last one
/// said.
pub struct Previews {
    /// The chats whose panes are on screen.
    pub visible: HashSet<String>,
    /// The chats whose preview is due, by [`Chat::key`], and when.
    pending: HashMap<String, (Chat, Instant)>,
    /// What the last preview of each chat said, and when it was sent, by [`Chat::key`].
    sent: HashMap<String, Sent>,
}

impl Previews {
    pub fn new(visible: HashSet<String>) -> Self {
        Self {
            visible,
            pending: HashMap::new(),
            sent: HashMap::new(),
        }
    }

    /// Whether a chat is shown whole: followed (`live`) and on screen.
    pub fn full(&self, live: Option<&HashSet<String>>, chat: &Chat) -> bool {
        shown_whole(live, &self.visible, chat)
    }

    /// The chat changed: its preview is due a moment from now, or [`EVERY`] after its last one.
    pub fn changed(&mut self, chat: Chat, now: Instant) {
        let key = chat.key().to_string();
        if self.pending.contains_key(&key) {
            return;
        }
        let soonest = now + SETTLE;
        let due = self
            .sent
            .get(&key)
            .map_or(soonest, |sent| soonest.max(sent.at + EVERY));
        self.pending.insert(key, (chat, due));
    }

    /// When the next preview is due.
    pub fn next_due(&self) -> Option<Instant> {
        self.pending.values().map(|(_, due)| *due).min()
    }

    /// The previews due by `now`, as events, each made from the agent as the Host holds it
    /// now. A chat whose preview says nothing its last one did not is sent none; a chat the
    /// Host no longer has is let go.
    pub fn take_due(&mut self, directory: &Directory, now: Instant) -> Vec<Value> {
        let due: Vec<String> = self
            .pending
            .iter()
            .filter(|(_, (_, due))| *due <= now)
            .map(|(key, _)| key.clone())
            .collect();
        let mut out = Vec::new();
        for key in due {
            let Some((chat, _)) = self.pending.remove(&key) else {
                continue;
            };
            let member = [&chat.agent_id, &chat.session]
                .into_iter()
                .filter(|name| !name.is_empty())
                .find_map(|name| directory.anyone(name));
            let Some(member) = member else {
                self.sent.remove(&key);
                continue;
            };
            let fields = crate::edge::preview_fields(directory, member);
            let chat = Chat {
                session: member.record.session.clone(),
                agent_id: member.record.agent_id.clone(),
            };
            if let Some(event) = self.patch(key, chat, fields, now) {
                out.push(event);
            }
        }
        out
    }

    /// The preview event of a chat whose fields are now `fields`, against its last one: none
    /// where nothing moved, and its message only where that moved.
    fn patch(&mut self, key: String, chat: Chat, fields: Value, now: Instant) -> Option<Value> {
        let last = self.sent.get(&key).map(|sent| &sent.fields);
        if last == Some(&fields) {
            return None;
        }
        let mut told = fields.clone();
        if let (Some(last), Some(told)) = (last, told.as_object_mut())
            && MESSAGE_FIELDS
                .iter()
                .all(|field| last[*field] == fields[*field])
        {
            for field in MESSAGE_FIELDS {
                told.remove(field);
            }
        }
        let event = json!({
            "type": "agent-roster",
            "kind": "preview",
            "agent_id": chat.agent_id,
            "session": chat.session,
            "fields": told,
        });
        self.sent.insert(
            key,
            Sent {
                chat,
                at: now,
                fields,
            },
        );
        Some(event)
    }

    /// The app reads the snapshot (the stream opened, or it was caught up): every chat's next
    /// preview says all of it, and none is due for what came before.
    pub fn resynced(&mut self) {
        self.pending.clear();
        self.sent.clear();
    }

    /// The chats on screen are now `visible`, and the stream follows `live`, from this moment.
    /// A chat now shown whole is previewed no more: the app reads it whole. Its next preview,
    /// once it is not shown whole again, says all of it, since what the app holds of it then
    /// came whole.
    pub fn moved(&mut self, live: Option<&HashSet<String>>, visible: HashSet<String>) {
        self.visible = visible;
        let visible = &self.visible;
        self.pending
            .retain(|_, (chat, _)| !shown_whole(live, visible, chat));
        self.sent
            .retain(|_, sent| !shown_whole(live, visible, &sent.chat));
    }
}

/// Whether a chat is shown whole: followed and on screen.
fn shown_whole(live: Option<&HashSet<String>>, visible: &HashSet<String>, chat: &Chat) -> bool {
    live.is_some_and(|live| named(live, &chat.session, &chat.agent_id))
        && named(visible, &chat.session, &chat.agent_id)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn chat(session: &str) -> Chat {
        Chat {
            session: session.to_string(),
            agent_id: format!("id-{session}"),
        }
    }

    fn names(names: &[&str]) -> HashSet<String> {
        names.iter().map(|name| (*name).to_string()).collect()
    }

    fn fields(state: &str, ts: i64, message: &str) -> Value {
        json!({
            "conversation_id": "c1", "head_revision": 3, "last_message_id": "m1",
            "last_message": message, "busy": state != "done", "latest_state": state,
            "latest_state_ts": ts, "status_text": null, "last_activity": ts,
        })
    }

    #[test]
    fn a_previewed_chats_changes_fold_into_its_preview_and_what_the_app_acts_on_goes_on() {
        let whole = |chat: &Chat| chat.session == "open";
        let about = |kind: &str, session: &str| {
            json!({"type": kind, "session": session, "agent_id": format!("id-{session}")})
                .to_string()
        };
        for kind in ["transcript-updated", "agent-activity", "agent-state"] {
            assert_eq!(
                fate(&about(kind, "rachel"), whole),
                Fate::Preview(chat("rachel")),
                "{kind}"
            );
            assert_eq!(fate(&about(kind, "open"), whole), Fate::Send, "{kind}");
        }
        // A send's receipt reaches the app wherever the chat is.
        let receipt = json!({"type": "agent-state", "session": "rachel",
            "agent_id": "id-rachel", "kind": "thinking", "client_msg_id": "c-1"});
        assert_eq!(
            fate(&receipt.to_string(), whole),
            Fate::SendAndPreview(chat("rachel"))
        );
        // The roster (availability too), notifications, queue, delivery and decisions go as
        // they are.
        for kind in [
            "agent-roster",
            "user-notification",
            "queue-updated",
            "delivery-updated",
            "artifact-updated",
            "audio",
        ] {
            assert_eq!(fate(&about(kind, "rachel"), whole), Fate::Send, "{kind}");
        }
        // An event that names no chat, and one whose words only mention a folded kind.
        assert_eq!(fate(r#"{"type":"agent-state"}"#, whole), Fate::Send);
        let quoted = json!({"type": "user-notification", "session": "rachel",
            "preview": "\"agent-state\""});
        assert_eq!(fate(&quoted.to_string(), whole), Fate::Send);
    }

    #[test]
    fn a_chat_is_shown_whole_where_it_is_followed_and_on_screen() {
        let previews = Previews::new(names(&["rachel"]));
        let rachel = chat("rachel");
        assert!(previews.full(Some(&names(&["*"])), &rachel));
        assert!(previews.full(Some(&names(&["id-rachel"])), &rachel));
        assert!(!previews.full(Some(&names(&["mike"])), &rachel));
        assert!(!previews.full(None, &rachel), "followed by nothing");
        let nothing_on_screen = Previews::new(HashSet::new());
        assert!(!nothing_on_screen.full(Some(&names(&["*"])), &rachel));
    }

    #[test]
    fn a_chats_previews_come_at_most_every_five_seconds_each_the_newest() {
        let start = Instant::now();
        let mut previews = Previews::new(HashSet::new());
        previews.changed(chat("rachel"), start);
        previews.changed(chat("rachel"), start + Duration::from_millis(100));
        assert_eq!(
            previews.next_due(),
            Some(start + SETTLE),
            "one, a moment on"
        );
        let first = previews
            .patch(
                "id-rachel".into(),
                chat("rachel"),
                fields("tool", 1, "hi"),
                start + SETTLE,
            )
            .unwrap();
        previews.pending.clear();
        assert_eq!(first["type"], "agent-roster");
        assert_eq!(first["kind"], "preview");
        assert!(first.get("event_id").is_none());
        assert_eq!(
            first["fields"],
            fields("tool", 1, "hi"),
            "the first says all"
        );
        // A change right after waits for the five seconds to pass.
        previews.changed(chat("rachel"), start + Duration::from_secs(1));
        assert_eq!(previews.next_due(), Some(start + SETTLE + EVERY));
        // A preview that says nothing new is not sent; one whose message stayed leaves it out.
        let later = start + Duration::from_secs(6);
        let same = previews.patch(
            "id-rachel".into(),
            chat("rachel"),
            fields("tool", 1, "hi"),
            later,
        );
        assert_eq!(same, None);
        let moved = previews
            .patch(
                "id-rachel".into(),
                chat("rachel"),
                fields("done", 2, "hi"),
                later,
            )
            .unwrap();
        assert_eq!(
            moved["fields"],
            json!({"busy": false, "latest_state": "done", "latest_state_ts": 2,
                "status_text": null, "last_activity": 2})
        );
        let answered = previews
            .patch(
                "id-rachel".into(),
                chat("rachel"),
                fields("done", 3, "bye"),
                later,
            )
            .unwrap();
        assert_eq!(answered["fields"], fields("done", 3, "bye"));
    }

    #[test]
    fn a_change_of_screen_stops_previews_of_a_chat_shown_whole_and_a_resync_forgets_them() {
        let start = Instant::now();
        let mut previews = Previews::new(HashSet::new());
        for session in ["rachel", "mike"] {
            previews.patch(
                format!("id-{session}"),
                chat(session),
                fields("tool", 1, "hi"),
                start,
            );
            previews.changed(chat(session), start + Duration::from_secs(1));
        }
        // Rachel comes on screen and is followed: hers is due no more, and once she leaves it
        // her next preview says all of it.
        let live = names(&["rachel", "mike"]);
        previews.moved(Some(&live), names(&["rachel"]));
        assert_eq!(previews.pending.len(), 1);
        assert!(previews.pending.contains_key("id-mike"));
        assert!(!previews.sent.contains_key("id-rachel"));
        assert!(previews.sent.contains_key("id-mike"));
        previews.moved(Some(&live), HashSet::new());
        let back = previews
            .patch(
                "id-rachel".into(),
                chat("rachel"),
                fields("tool", 1, "hi"),
                start,
            )
            .unwrap();
        assert_eq!(back["fields"], fields("tool", 1, "hi"));
        // A catch-up: the app reads the snapshot, so nothing is due and the next says all.
        previews.resynced();
        assert_eq!(previews.next_due(), None);
        let after = previews
            .patch(
                "id-mike".into(),
                chat("mike"),
                fields("tool", 1, "hi"),
                start,
            )
            .unwrap();
        assert_eq!(after["fields"], fields("tool", 1, "hi"));
    }
}
