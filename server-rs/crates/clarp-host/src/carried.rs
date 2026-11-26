//! The rows a `transcript-updated` stands for, carried in it to the connections that ask for
//! them (`/events?rows=1`, feature `events_rows`, issue 879), so that a client need not read
//! `/log` after each one.
//!
//! The rows are read off the store as the event is fanned out, once for every connection that
//! asks: the first to ask reads, the others wait for that read. A read that takes longer than
//! [`WITHIN`] (a disk that stalls) is not waited for: the event goes as it always did, and the
//! client reads `/log`. A slow store never holds a live event longer than that.

use std::collections::VecDeque;
use std::future::Future;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use serde_json::Value;
use tokio::sync::OnceCell;

/// How long a connection waits for the rows before it sends the event without them.
pub const WITHIN: Duration = Duration::from_millis(200);
/// How many rows an event carries at most.
pub const ROWS_MAX: usize = 20;
/// How large the rows an event carries may be, as JSON.
pub const BYTES_MAX: usize = 16 * 1024;
/// How many recent reads (an event and a set of options each) are kept for the connections that
/// ask after the first.
const KEPT: usize = 256;

/// The `/log` options the rows are read with, as a connection asked for them (feature
/// `events_rows_options`, issue 928): whether automated rows are in (`rows_automated`, as
/// `include_automated`), and whether coordination follows the chat's setting
/// (`rows_coordination=auto`, as `coordination=auto`). Without them the rows are as `events_rows`
/// first read them: automated rows in, no coordination.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Options {
    pub automated: bool,
    pub coordination: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            automated: true,
            coordination: false,
        }
    }
}

/// An event with its rows, the chat it is of, and the coordination visibility its rows were read
/// under (with `rows_coordination=auto`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Carried {
    pub json: Arc<str>,
    /// The chat the event is of.
    pub session: String,
    pub visibility: Option<String>,
}

/// The event with its rows, by the number of the event and the options read with: one read for
/// every connection that asks with the same options.
type Slot = Arc<OnceCell<Option<Carried>>>;

#[derive(Default)]
pub struct Reads {
    slots: Mutex<VecDeque<((u64, Options), Slot)>>,
}

impl Reads {
    /// The event numbered `id` with its rows read with `options`, as `read` makes it: read once
    /// for every connection that asks with those options, and waited for at most `within`.
    /// `None` where it cannot carry them, or not in time.
    pub async fn get<F, Fut>(
        &self,
        id: u64,
        options: Options,
        within: Duration,
        read: F,
    ) -> Option<Carried>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Option<Carried>>,
    {
        let key = (id, options);
        let slot = {
            let mut slots = self.slots.lock().unwrap_or_else(PoisonError::into_inner);
            match slots.iter().find(|(kept, _)| *kept == key) {
                Some((_, slot)) => slot.clone(),
                None => {
                    let slot = Slot::default();
                    slots.push_back((key, slot.clone()));
                    while slots.len() > KEPT {
                        slots.pop_front();
                    }
                    slot
                }
            }
        };
        // A connection that stops waiting drops its part of the read; another one that still
        // waits reads again.
        let made = slot.get_or_init(read);
        tokio::time::timeout(within, made)
            .await
            .ok()
            .cloned()
            .flatten()
    }
}

/// Puts the rows of a `/log` answer (a delta after the event's `rows_from`) into the event,
/// under `rows`, with the revision they reach under `rows_latest`, where the answer holds all
/// of them, they are few and small, and they are of the event's conversation. Otherwise the
/// event is left as it is and the answer is false: the client reads `/log`. With `coordination`
/// (the rows were read with `coordination=auto`) it also carries what `/log` says of it.
pub fn carry(event: &mut Value, mut answer: Value, coordination: bool) -> bool {
    let said = |field: &str| answer[field].as_bool();
    let whole = said("has_more") == Some(false)
        && said("replace_required") == Some(false)
        && said("missing") == Some(false)
        && answer["conversation_id"].is_string()
        && answer["conversation_id"] == event["conversation_id"];
    let few = answer["turns"]
        .as_array()
        .is_some_and(|rows| rows.len() <= ROWS_MAX);
    if !whole || !few {
        return false;
    }
    let rows = answer["turns"].take();
    if rows.to_string().len() > BYTES_MAX {
        return false;
    }
    event["rows"] = rows;
    event["rows_latest"] = answer["latest_revision"].take();
    // What that read says of coordination, under `/log`'s own names but for the rows: the
    // chat's policy, and the run of hidden turns after the last row.
    if coordination {
        event["rows_coordination"] = answer["coordination"].take();
        if answer.get("hidden_after").is_some() {
            event["hidden_after"] = answer["hidden_after"].take();
        }
    }
    true
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn event() -> Value {
        json!({"type": "transcript-updated", "session": "s", "conversation_id": "c1",
               "head_revision": 12, "rows_from": 10})
    }

    fn answer(rows: usize) -> Value {
        let turns: Vec<Value> = (0..rows)
            .map(|n| json!({"id": format!("r{n}"), "revision": 11 + n, "text": "short"}))
            .collect();
        json!({"conversation_id": "c1", "turns": turns, "latest_revision": 10 + rows,
               "has_more": false, "replace_required": false, "missing": false})
    }

    #[test]
    fn few_small_rows_of_the_same_conversation_are_carried() {
        let mut told = event();
        assert!(carry(&mut told, answer(2), false));
        assert_eq!(told["rows"].as_array().unwrap().len(), 2);
        assert_eq!(told["rows_latest"], 12);
        assert_eq!(told["rows_from"], 10);
    }

    #[test]
    fn rows_that_cannot_be_carried_whole_leave_the_event_as_it_was() {
        let cases: Vec<(&str, Value)> = vec![
            ("more than the page held", {
                let mut more = answer(2);
                more["has_more"] = json!(true);
                more
            }),
            ("more than twenty rows", answer(ROWS_MAX + 1)),
            ("more than 16 KB", {
                let mut large = answer(1);
                large["turns"][0]["text"] = json!("x".repeat(BYTES_MAX));
                large
            }),
            ("a conversation the client must read anew", {
                let mut replace = answer(2);
                replace["replace_required"] = json!(true);
                replace
            }),
            ("another conversation", {
                let mut other = answer(2);
                other["conversation_id"] = json!("c2");
                other
            }),
            ("no conversation", {
                let mut missing = answer(0);
                missing["missing"] = json!(true);
                missing
            }),
            ("an answer that says nothing of it", json!({"turns": []})),
        ];
        for (case, answer) in cases {
            let mut told = event();
            assert!(!carry(&mut told, answer, false), "{case}");
            assert_eq!(told, event(), "{case}: the event is as it was");
        }
        let mut told = event();
        assert!(
            carry(&mut told, answer(ROWS_MAX), false),
            "twenty rows are carried"
        );
    }

    #[test]
    fn rows_read_with_coordination_carry_what_log_says_of_it() {
        let mut answered = answer(1);
        answered["coordination"] = json!({"hidden": true, "source": "chat", "visibility": "hide"});
        answered["hidden_after"] = json!({"count": 2, "from_revision": 12});
        let mut told = event();
        assert!(carry(&mut told, answered.clone(), true));
        assert_eq!(
            told["rows_coordination"],
            json!({"hidden": true, "source": "chat", "visibility": "hide"})
        );
        assert_eq!(told["hidden_after"]["count"], 2);
        // Read without coordination, nothing of it is carried.
        let mut plain = event();
        assert!(carry(&mut plain, answered, false));
        assert!(plain.get("rows_coordination").is_none() && plain.get("hidden_after").is_none());
    }

    fn carried(json: &str) -> Option<Carried> {
        Some(Carried {
            json: Arc::from(json),
            session: "s".into(),
            visibility: Some("show".into()),
        })
    }

    #[tokio::test]
    async fn one_read_serves_every_connection_that_asks_with_the_same_options() {
        let reads = Reads::default();
        let made = Arc::new(AtomicUsize::new(0));
        let read = |made: Arc<AtomicUsize>| {
            move || async move {
                made.fetch_add(1, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(20)).await;
                carried("{\"rows\":[]}")
            }
        };
        let apps = Options {
            automated: false,
            coordination: true,
        };
        let (a, b) = tokio::join!(
            reads.get(7, apps, WITHIN, read(made.clone())),
            reads.get(7, apps, WITHIN, read(made.clone())),
        );
        assert_eq!(a, carried("{\"rows\":[]}"));
        assert_eq!(a, b);
        assert_eq!(made.load(Ordering::SeqCst), 1, "read once");
        // Other options are a read of their own, and so is another event.
        reads
            .get(7, Options::default(), WITHIN, read(made.clone()))
            .await;
        assert_eq!(made.load(Ordering::SeqCst), 2);
        reads.get(8, apps, WITHIN, read(made.clone())).await;
        assert_eq!(made.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn a_read_slower_than_its_bound_is_not_waited_for() {
        let reads = Reads::default();
        let started = tokio::time::Instant::now();
        let slow = || async {
            tokio::time::sleep(Duration::from_secs(5)).await;
            carried("{}")
        };
        assert_eq!(reads.get(9, Options::default(), WITHIN, slow).await, None);
        let waited = started.elapsed();
        assert!(
            waited >= WITHIN && waited < WITHIN + Duration::from_millis(300),
            "{waited:?}"
        );
        // A read that cannot carry the rows is not carried either.
        assert_eq!(
            reads
                .get(10, Options::default(), WITHIN, || async { None })
                .await,
            None
        );
    }
}
