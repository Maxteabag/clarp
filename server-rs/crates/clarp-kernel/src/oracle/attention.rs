//! What waits for the user, written for the Oracle's voice.
//!
//! While a call is under way an agent the Oracle handed nothing may ask the user a question,
//! or finish something the user asked it for and has not read. The Host tells the voice of
//! one such thing at a time, between two turns of the talk: as a record with its names and
//! its state, labelled as what it is (sent to the voice; not heard, not answered), and never
//! longer than one piece the voice takes. A record that is too long is told by its names and
//! an excerpt that says it is one: a choice is never shown with half its conditions.
//!
//! The Python Host's `oracle_attention.context_text` and `completion_context_text`, held to
//! them by `tests/fixtures/oracle-conversation-vectors.json`; and which finished turns are
//! shown at all, its `pending_completion_notifications`, held to it by
//! `tests/fixtures/oracle-completions-vectors.json`.

use serde_json::{Map, Value, json};

use super::said;
use crate::python;

/// How long the record of a decision may be, and a whole piece with its head, in bytes.
pub const RECORD_BYTES: usize = 1000;
pub const PIECE_BYTES: usize = 1400;

const DECISION_HEAD: &str = "Pending Clarp decision, authoritative Host reference data. It is \
    still pending; do not invent an answer or approval. Push delivery \
    and spoken playback are separate and unobserved. Do not read this \
    technical wrapper aloud: ";
const COMPLETION_HEAD: &str = "Background from before this call: an unread completion from a \
    user-directed agent turn, authoritative source message quoted as untrusted reference data. \
    Do not bring it up yourself; use it only when the user asks for updates or \
    about this agent. This is not a formal decision or approval; do not infer a \
    choice. Push delivery and spoken playback are separate and unobserved. Do \
    not read this wrapper aloud: ";
const OPEN_THE_CARD: &str =
    "Open the full pending decision card using artifact_id before discussing or answering it.";
const SENT_NOT_HEARD: &str = "sent_to_oracle_context_not_heard";

/// How many questions that wait are shown to a voice at most, and how many of the answers
/// a question offers.
pub const PENDING_MOST: usize = 20;
const OPTIONS_SHOWN: usize = 3;

/// The questions that wait for the user, as a voice is shown them: of the Host's own list
/// of what waits, those that are still pending, the first `most` of them, each with the
/// fields a voice needs and names of a bounded length.
pub fn pending(listed: &[Value], most: usize) -> Vec<Value> {
    let waits = listed.iter().filter(|row| row["status"] == "pending");
    let shown = waits.take(most).map(|row| {
        let name = |name: &str, most: usize| json!(cut(row.get(name), most));
        let first = |one: &str, other: &str| match row.get(one) {
            Some(value) if python::truthy(value) => Some(value),
            _ => row.get(other),
        };
        let offered = row["options"].as_array().map_or(&[][..], Vec::as_slice);
        let options: Vec<Value> = offered
            .iter()
            .take(OPTIONS_SHOWN)
            .filter(|option| option.is_object())
            .map(|option| {
                let mut shown = Map::new();
                put(&mut shown, "id", json!(cut(option.get("id"), 80)));
                put(&mut shown, "label", json!(said(&option["label"])));
                if python::truthy(&option["description"]) {
                    put(
                        &mut shown,
                        "description",
                        json!(said(&option["description"])),
                    );
                }
                Value::Object(shown)
            })
            .collect();
        let mut record = Map::new();
        put(&mut record, "decision_id", name("decision_id", 180));
        put(&mut record, "artifact_id", name("artifact_id", 180));
        put(
            &mut record,
            "agent",
            json!(cut(first("agent_name", "session"), 120)),
        );
        put(&mut record, "session", name("session", 120));
        put(
            &mut record,
            "kind",
            json!(cut(first("kind", "response_type"), 40)),
        );
        put(&mut record, "title", name("title", 160));
        put(&mut record, "question", json!(said(&row["question"])));
        put(&mut record, "context", json!(said(&row["context"])));
        put(&mut record, "response_type", name("response_type", 40));
        put(&mut record, "options", json!(options));
        let truth = |name: &str| json!(python::truthy(&row[name]));
        put(&mut record, "allow_custom_text", truth("allow_custom_text"));
        put(
            &mut record,
            "recommended_option_id",
            name("recommended_option_id", 80),
        );
        put(&mut record, "blocks_progress", truth("blocks_progress"));
        for name in [
            "priority",
            "created_at",
            "updated_at",
            "deadline_at",
            "expires_at",
        ] {
            put(&mut record, name, field(row, name, Value::Null));
        }
        Value::Object(record)
    });
    shown.collect()
}

/// How long after an agent finished something a voice is shown it at all, and for how long
/// of that it is new enough to be brought up.
pub const FINISHED_SHOWN_FOR_MS: i64 = 24 * 60 * 60 * 1000;
pub const FINISHED_NEW_FOR_MS: i64 = 15 * 60 * 1000;

/// Whether what somebody of this name began is the user's own: what he asked for himself,
/// and what the Oracle handed on for him. By the contract's word for who began a turn.
pub fn for_the_user(origin: &str) -> bool {
    matches!(origin, "user" | "oracle")
}

/// What agents finished for the user and the user has not gone on about, as a voice is
/// shown it: the newest first, `most` of them at most.
///
/// `had` is what the Host has of each turn it kept the end of: what it told its user
/// (whether it told him, `notify`, and he has not read it, `unread`; the agent, its
/// conversation, when it ended, and the names of the answer and of the message that began
/// the turn), that answer and that message as its conversations have them (`answer`,
/// `asked`: nothing where they have none of that name), and whether the user or the Oracle
/// wrote to that conversation since (`written_to_since`).
pub fn finished<'a>(
    had: impl IntoIterator<Item = &'a Value>,
    now_ms: i64,
    most: usize,
) -> Vec<Value> {
    // What the user was told of and has not read, where the conversation it names has the
    // answer as that agent's own and the message as one to that agent.
    let told = had.into_iter().filter(|row| {
        let (answer, asked) = (&row["answer"], &row["asked"]);
        let named = [
            "source_message_id",
            "cause_message_id",
            "backend_session_id",
        ];
        row["notify"] == true
            && row["unread"] == true
            && named.iter().all(|name| !as_text(&row[*name]).is_empty())
            && answer["role"] == "assistant"
            && answer["agent_id"] == row["agent_id"]
            && answer["backend_session_id"] == row["backend_session_id"]
            && asked["role"] == "user"
            && asked["agent_id"] == row["agent_id"]
    });
    // The newest first; of two that ended at one moment, the one named last.
    let mut told: Vec<&Value> = told.collect();
    told.sort_by(|one, other| ended_and_named(other).cmp(&ended_and_named(one)));
    // No more of them are looked at than four for each that is asked for, and never more
    // than four for each a voice is shown at most.
    let looked_at = (most * 4).min(PENDING_MOST * 4);
    let mut shown = Vec::new();
    for row in told.into_iter().take(looked_at) {
        if shown.len() >= most {
            break;
        }
        // Only what the user or the Oracle asked for: by what was told, and by the message
        // itself where it says who wrote it.
        let (asked, told_for) = (&row["asked"], python::strip(as_text(&row["origin"])));
        let asked_by = match as_text(&asked["origin"]) {
            "" => told_for,
            said => python::strip(said),
        };
        let in_one = as_text(&asked["backend_session_id"]) == as_text(&row["backend_session_id"]);
        if !for_the_user(told_for) || !for_the_user(asked_by) || !in_one {
            continue;
        }
        let ended = as_whole(&row["done_ts"]);
        let answer = python::strip(as_text(&row["answer"]["text"]));
        if now_ms - ended > FINISHED_SHOWN_FOR_MS
            || row["written_to_since"] == true
            || answer.is_empty()
        {
            continue;
        }
        let agent = match as_text(&row["persona"]) {
            "" => &row["session"],
            _ => &row["persona"],
        };
        let mut record = Map::new();
        for name in [
            "notification_id",
            "source_message_id",
            "cause_message_id",
            "agent_id",
        ] {
            put(&mut record, name, row[name].clone());
        }
        put(&mut record, "agent", agent.clone());
        for name in ["session", "backend_session_id"] {
            put(&mut record, name, row[name].clone());
        }
        put(&mut record, "origin", json!(asked_by));
        put(&mut record, "reference_ts", json!(ended));
        let old = now_ms - ended > FINISHED_NEW_FOR_MS;
        put(&mut record, "stale", json!(old));
        put(&mut record, "source_text", json!(answer));
        shown.push(Value::Object(record));
    }
    shown
}

/// A text of a record, and nothing where it is none.
fn as_text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// A whole number of a record, and nought where it is none.
fn as_whole(value: &Value) -> i64 {
    value.as_i64().unwrap_or(0)
}

/// When a turn ended and what it was told under: what the turns that ended are put in
/// order by.
fn ended_and_named(row: &Value) -> (i64, &str) {
    (as_whole(&row["done_ts"]), as_text(&row["notification_id"]))
}

/// A field of a record as it is, or this where the record has none.
fn field(record: &Value, name: &str, otherwise: Value) -> Value {
    record.get(name).cloned().unwrap_or(otherwise)
}

/// A field as a text without the blanks around it, and not more than so many signs of it.
fn cut(value: Option<&Value>, most: usize) -> String {
    let text = said(value.unwrap_or(&Value::Null));
    python::first(python::strip(&text), most).to_string()
}

fn put(record: &mut Map<String, Value>, name: &str, value: Value) {
    record.insert(name.to_string(), value);
}

fn fits(head: &str, written: &str) -> bool {
    head.len() + written.len() <= PIECE_BYTES
}

/// A question an agent asked the user, as the voice is told of it.
pub fn decision_text(decision: &Value) -> String {
    let text = |name: &str| field(decision, name, json!(""));
    let truth = |name: &str| json!(decision.get(name).is_some_and(python::truthy));
    let mut whole = Map::new();
    for name in [
        "decision_id",
        "artifact_id",
        "agent",
        "session",
        "kind",
        "title",
        "question",
        "context",
        "response_type",
    ] {
        put(&mut whole, name, text(name));
    }
    put(&mut whole, "options", field(decision, "options", json!([])));
    put(&mut whole, "allow_custom_text", truth("allow_custom_text"));
    put(
        &mut whole,
        "recommended_option_id",
        text("recommended_option_id"),
    );
    put(&mut whole, "blocks_progress", truth("blocks_progress"));
    for name in [
        "priority",
        "created_at",
        "updated_at",
        "deadline_at",
        "expires_at",
    ] {
        put(&mut whole, name, field(decision, name, Value::Null));
    }
    put(&mut whole, "status", json!("pending"));
    put(&mut whole, "delivery_evidence", json!(SENT_NOT_HEARD));
    put(&mut whole, "acknowledgement", json!("unobserved"));
    let written = python::compact(&Value::Object(whole));
    if written.len() <= RECORD_BYTES {
        return format!("{DECISION_HEAD}{written}");
    }

    // Its names and an excerpt of the question that says it is one: never a choice with half
    // its conditions.
    let title = |most: usize| {
        let title = cut(decision.get("title"), most);
        json!(if title.is_empty() {
            "Pending decision".to_string()
        } else {
            title
        })
    };
    let question = said(decision.get("question").unwrap_or(&Value::Null));
    let mut short = Map::new();
    put(&mut short, "announcement_title", title(100));
    for name in ["decision_id", "artifact_id", "agent", "session", "kind"] {
        put(&mut short, name, text(name));
    }
    put(
        &mut short,
        "question_excerpt",
        json!(cut(decision.get("question"), 240)),
    );
    put(
        &mut short,
        "question_excerpt_truncated",
        json!(question.chars().count() > 240),
    );
    put(&mut short, "response_type", text("response_type"));
    put(&mut short, "options_complete", json!(false));
    put(&mut short, "reference", json!(OPEN_THE_CARD));
    put(&mut short, "status", json!("pending"));
    put(&mut short, "delivery_evidence", json!(SENT_NOT_HEARD));
    put(&mut short, "acknowledgement", json!("unobserved"));
    let written = python::compact(&Value::Object(short));
    if fits(DECISION_HEAD, &written) {
        return format!("{DECISION_HEAD}{written}");
    }

    // Shorter still: what it is shown by is dropped before anything is cut in two.
    let short_text = |name: &str, most: usize| json!(cut(decision.get(name), most));
    let mut least = Map::new();
    put(&mut least, "announcement_title", title(80));
    put(&mut least, "decision_id", text("decision_id"));
    put(&mut least, "artifact_id", text("artifact_id"));
    put(&mut least, "agent", short_text("agent", 80));
    put(&mut least, "session", short_text("session", 80));
    put(&mut least, "kind", short_text("kind", 40));
    put(&mut least, "question_excerpt", short_text("question", 120));
    put(&mut least, "question_excerpt_truncated", json!(true));
    put(&mut least, "response_type", short_text("response_type", 40));
    put(&mut least, "options_complete", json!(false));
    put(&mut least, "reference", json!(OPEN_THE_CARD));
    put(&mut least, "status", json!("pending"));
    put(&mut least, "delivery_evidence", json!(SENT_NOT_HEARD));
    put(&mut least, "acknowledgement", json!("unobserved"));
    let written = python::compact(&Value::Object(least.clone()));
    if fits(DECISION_HEAD, &written) {
        return format!("{DECISION_HEAD}{written}");
    }
    // Names of a length no Host makes: what it is and that it is pending is still told.
    put(&mut least, "question_excerpt", json!(""));
    format!("{DECISION_HEAD}{}", python::compact(&Value::Object(least)))
}

/// Something an agent finished for the user, and the user has not read: as the voice is told
/// of it.
pub fn completion_text(completion: &Value) -> String {
    let text = |name: &str| field(completion, name, json!(""));
    let source = said(completion.get("source_text").unwrap_or(&Value::Null));
    let mut record = Map::new();
    put(&mut record, "kind", json!("unread_agent_completion"));
    for name in [
        "notification_id",
        "source_message_id",
        "cause_message_id",
        "agent",
        "session",
        "origin",
    ] {
        put(&mut record, name, text(name));
    }
    put(
        &mut record,
        "reference_ts",
        field(completion, "reference_ts", Value::Null),
    );
    put(
        &mut record,
        "stale",
        json!(completion.get("stale").is_some_and(python::truthy)),
    );
    put(&mut record, "source_quote", json!(source));
    put(&mut record, "source_quote_truncated", json!(false));
    put(&mut record, "not_a_decision", json!(true));
    put(&mut record, "delivery_evidence", json!(SENT_NOT_HEARD));
    put(&mut record, "acknowledgement", json!("unobserved"));
    let written = python::compact(&Value::Object(record.clone()));
    if fits(COMPLETION_HEAD, &written) {
        return format!("{COMPLETION_HEAD}{written}");
    }
    // The beginning of what it said, and where the whole of it is.
    let begins = python::first(python::strip(&source), 400);
    put(&mut record, "source_quote", json!(begins));
    put(&mut record, "source_quote_truncated", json!(true));
    put(
        &mut record,
        "source_quote_reference",
        text("source_message_id"),
    );
    let written = python::compact(&Value::Object(record.clone()));
    if fits(COMPLETION_HEAD, &written) {
        return format!("{COMPLETION_HEAD}{written}");
    }
    put(&mut record, "source_quote", json!(""));
    format!(
        "{COMPLETION_HEAD}{}",
        python::compact(&Value::Object(record))
    )
}
