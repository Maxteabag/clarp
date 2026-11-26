//! The hub: every change the Host tells its clients about is numbered here and fanned out.
//!
//! An event is a wake-up, not the data: it says that something changed, and the client asks what.
//! That is why the hub may forget. It keeps the last few minutes so that a client that drops for
//! a moment misses nothing; a client that was away for longer, or across a restart of the Host,
//! is told to look at everything again, and the conversations themselves tell it what is new.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, OnceLock, PoisonError, Weak};

use serde_json::{Value, json};
use tokio::sync::{broadcast, watch};

use crate::fleet::Directory;

/// How long an event is kept for clients that reconnect.
const KEEP_MS: i64 = 5 * 60 * 1000;
const KEEP_MAX: usize = 20_000;
/// How far a listening client may fall behind before it is cut off and has to reconnect.
const BEHIND_MAX: usize = 4_096;
/// How many ended turns wait at most for the experience watcher to take them, the newest kept.
const TURN_ENDS_MAX: usize = 4_096;

/// An event as it was sent: its number, and its JSON, written once for everyone.
#[derive(Debug)]
pub struct Published {
    pub id: u64,
    pub at_ms: i64,
    pub json: String,
}

struct Recent {
    /// The first number this run of the Host gave out: a number below it is an earlier run's.
    first_id: u64,
    next_id: u64,
    events: VecDeque<Arc<Published>>,
}

/// Where a connection takes up the events after the last one it was sent (feature
/// `compact_resume`, issue 882): the kept events after it, the receiver for everything from
/// here on, with no gap and no repeat between them, and whether the kept events reach back to
/// it (`truncated` when they do not, or when the number is another run's).
pub struct Resumed {
    pub missed: Vec<Arc<Published>>,
    pub live: broadcast::Receiver<Arc<Published>>,
    pub truncated: bool,
    /// The number of the newest event given out: where a connection that was given nothing
    /// stands.
    pub head: u64,
}

/// What an agent's turn shows right now, as it is sent to those who asked to be shown
/// (`/events?live=`, `docs/live-items.md`). It has no number and is never kept: a client that
/// missed some asks for all of it (`GET /live`).
#[derive(Debug)]
pub struct Shown {
    pub session: String,
    pub agent_id: String,
    /// The event whole, for a connection that follows this agent.
    pub whole: String,
    /// The same with only what a list of agents shows of it: the status line and the turn.
    /// `None` when it has nothing of that: a connection that does not follow the agent is
    /// sent nothing then.
    pub summary: Option<String>,
}

impl Shown {
    /// What a connection is sent of it. `wanted` are the sessions and agents it follows,
    /// with `*` for every one.
    pub fn for_one(&self, wanted: &HashSet<String>) -> Option<&str> {
        let named = |name: &String| !name.is_empty() && wanted.contains(name);
        if wanted.contains("*") || named(&self.session) || named(&self.agent_id) {
            Some(&self.whole)
        } else {
            self.summary.as_deref()
        }
    }
}

pub struct Hub {
    pub capture: Option<Arc<crate::diagnostics::Capture>>,
    recent: Mutex<Recent>,
    live: broadcast::Sender<Arc<Published>>,
    /// What agents' turns show as they run, for the connections that asked for it.
    shown: broadcast::Sender<Arc<Shown>>,
    /// The rows each recent `transcript-updated` stands for, read once for every connection that
    /// asked for them (`rows=1`, feature `events_rows`).
    pub carried: crate::carried::Reads,
    /// The open streams whose followed chats may be changed (feature `events_live_change`).
    pub streams: Arc<crate::live_change::Streams>,
    /// The name of this run of the Host for those who watch a turn.
    epoch: String,
    /// How many requests of each agent wait for the user: what the apps' badge counts.
    waiting: Mutex<BTreeMap<String, usize>>,
    /// What each agent's turn shows now, for who follows every agent inside this Host (the
    /// chip a phone shows of the fleet): the status alone, never what the turn wrote.
    statuses: broadcast::Sender<Arc<Value>>,
    /// Counts the changes of what waits for the user: who watches it wakes with each.
    badge: tokio::sync::watch::Sender<u64>,
    /// What adds an agent's profile to the roster's events about it (feature
    /// `voice_profile`), once the Host has its speaker and its ears.
    pub profiles: crate::profile::Slot,
    /// A queue event resolves names when it is published. The fleet owns the roster.
    queue_roster: OnceLock<Weak<watch::Receiver<Arc<Directory>>>>,
    /// The turns that ended, as their agents judged them, until the experience watcher takes
    /// them.
    turn_ends: Mutex<VecDeque<crate::experience_watch::Ended>>,
    /// The Host is stopping: every event stream ends now, so that its apps reconnect to the
    /// Host that follows at once rather than when this process is gone (issue 880).
    closing: watch::Sender<bool>,
    /// What every event stream is sent as its last frame when the Host stops (`host-updating`,
    /// feature `agent_availability`).
    farewell: Mutex<Option<Arc<str>>>,
    /// When this run of the Host started.
    started_ms: i64,
    /// The agents whose status or queue moved since availability was last looked at, and the
    /// way to wake whoever looks (`crate::availability::run`).
    availability_moved: Mutex<HashSet<String>>,
    availability_woken: tokio::sync::Notify,
    /// The same account observation serves both agent reads and live availability.
    pub(crate) availability_quota: watch::Sender<Arc<crate::quota::Rows>>,
    /// The numbers its events may have, reserved on disk (`Reserved`); none for a hub of a test.
    reserved: Option<Arc<Reserved>>,
}

impl Hub {
    /// A hub whose numbers start above every number a hub started earlier gave out: the time
    /// leads, so a restart never reuses one.
    pub fn new(now_ms: i64) -> Self {
        let (live, _) = broadcast::channel(BEHIND_MAX);
        let (shown, _) = broadcast::channel(BEHIND_MAX);
        let (statuses, _) = broadcast::channel(BEHIND_MAX);
        let (badge, _) = tokio::sync::watch::channel(0);
        let (closing, _) = watch::channel(false);
        let seconds = u64::try_from(now_ms / 1_000).unwrap_or(0);
        Self {
            capture: None,
            carried: crate::carried::Reads::default(),
            streams: Arc::default(),
            shown,
            statuses,
            badge,
            // The moment it started names a run: no two runs of a Host start in one. Twelve
            // hex signs, as the Python Host's name of a run has.
            epoch: format!("{now_ms:012x}"),
            recent: Mutex::new(Recent {
                first_id: (seconds << 20).max(1),
                next_id: (seconds << 20).max(1),
                events: VecDeque::new(),
            }),
            live,
            waiting: Mutex::new(BTreeMap::new()),
            profiles: crate::profile::Slot::new(),
            queue_roster: OnceLock::new(),
            turn_ends: Mutex::new(VecDeque::new()),
            closing,
            farewell: Mutex::new(None),
            started_ms: now_ms,
            availability_moved: Mutex::default(),
            availability_woken: tokio::sync::Notify::new(),
            availability_quota: watch::Sender::new(Arc::new(crate::quota::Rows::unasked())),
            reserved: None,
        }
    }

    /// This run's events take their numbers from a reservation kept in the file at `path`:
    /// from at or above the end of the last one any run made, and none before its own first block
    /// is on disk (`Reserved`, issue 935).
    #[must_use]
    pub fn reserving(self, path: std::path::PathBuf) -> Self {
        self.reserving_in_blocks(path, BLOCK)
    }

    fn reserving_in_blocks(mut self, path: std::path::PathBuf, block: u64) -> Self {
        let reserved = Arc::new(Reserved::new(path, block));
        let floor = match reserved.read() {
            Ok(floor) => floor,
            Err(why) => {
                // The floor is unknown: the file is kept, nothing is reserved over it, and the
                // numbers of this run go unreserved, from the time.
                use std::sync::atomic::Ordering;
                reserved.unknown.store(true, Ordering::Release);
                reserved.lapsed.store(true, Ordering::Release);
                reserved.refused.store(true, Ordering::Relaxed);
                eprintln!(
                    "clarp-host: the reservation of event numbers at {} cannot be read ({why}): it is \
                     kept as it is, and this run numbers its events unreserved: a client that \
                     resumes across its next restart may be told it missed nothing",
                    reserved.path.display()
                );
                self.reserved = Some(reserved);
                return self;
            }
        };
        let recent = self.recent.get_mut().unwrap_or_else(PoisonError::into_inner);
        if floor > recent.first_id && recent.events.is_empty() {
            recent.first_id = floor;
            recent.next_id = floor;
        }
        let first = recent.next_id;
        if let Err(why) = reserved.reserve(first.saturating_add(block)) {
            reserved.refused(&why);
        }
        self.reserved = Some(reserved);
        self
    }

    /// When this run of the Host started.
    pub fn started_ms(&self) -> i64 {
        self.started_ms
    }

    /// An agent's status or queue moved: its availability is looked at again. Does not wait.
    pub(crate) fn availability_moved(&self, agent_id: &str) {
        let mut moved = self
            .availability_moved
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if moved.insert(agent_id.to_owned()) && moved.len() == 1 {
            self.availability_woken.notify_one();
        }
    }

    /// The agents whose status or queue moved since this was last asked.
    pub(crate) fn take_availability_moved(&self) -> HashSet<String> {
        std::mem::take(
            &mut *self
                .availability_moved
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        )
    }

    /// Resolves when an agent's status or queue moved ([`Hub::availability_moved`]).
    pub(crate) async fn availability_wait(&self) {
        self.availability_woken.notified().await;
    }

    /// The Host is stopping, with what every event stream is sent before it ends.
    pub fn close_streams_with(&self, farewell: &Value) {
        *self.farewell.lock().unwrap_or_else(PoisonError::into_inner) =
            Some(Arc::from(farewell.to_string()));
        self.close_streams();
    }

    /// What an event stream is sent as its last frame, when the Host said one.
    pub(crate) fn farewell(&self) -> Option<Arc<str>> {
        self.farewell
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// The Host is stopping: every event stream open now, or opened after, ends (issue 880).
    pub fn close_streams(&self) {
        self.closing.send_replace(true);
    }

    /// Resolves once the Host is stopping ([`Hub::close_streams`]), at once when it already
    /// is. What an event stream waits on beside its events.
    pub fn closing(&self) -> impl std::future::Future<Output = ()> + Send + 'static + use<> {
        let mut closing = self.closing.subscribe();
        async move {
            // A sender that is gone is a Host that is gone: the stream ends then too.
            let _ = closing.wait_for(|closing| *closing).await;
        }
    }

    /// An agent's turn ended: kept for the experience watcher. Does not wait.
    pub(crate) fn turn_ended(&self, ended: crate::experience_watch::Ended) {
        let mut ends = self
            .turn_ends
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if ends.len() >= TURN_ENDS_MAX {
            ends.pop_front();
        }
        ends.push_back(ended);
    }

    /// The turns that ended since they were last taken.
    pub(crate) fn take_turn_ends(&self) -> Vec<crate::experience_watch::Ended> {
        let mut ends = self
            .turn_ends
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        std::mem::take(&mut *ends).into()
    }

    pub(crate) fn queue_roster(
        &self,
        roster: &Arc<watch::Receiver<Arc<Directory>>>,
    ) -> Result<(), &'static str> {
        self.queue_roster
            .set(Arc::downgrade(roster))
            .map_err(|_| "the queue roster is already attached")
    }

    pub(crate) fn queue_directory(&self) -> Option<Arc<Directory>> {
        let roster = self.queue_roster.get()?.upgrade()?;
        let directory = roster.borrow().clone();
        Some(directory)
    }

    /// An agent says how many of its requests wait for the user. Answers how many wait in
    /// all: the count the apps are told.
    pub fn waiting_for_user(&self, agent_id: &str, count: usize) -> usize {
        let mut waiting = self.waiting.lock().unwrap_or_else(PoisonError::into_inner);
        let before = waiting.get(agent_id).copied().unwrap_or(0);
        if count == 0 {
            waiting.remove(agent_id);
        } else {
            waiting.insert(agent_id.to_string(), count);
        }
        if before != count {
            self.badge.send_modify(|changes| *changes += 1);
        }
        waiting.values().sum()
    }

    /// What waits for the user changed apart from the agents' requests: what reached the owner
    /// of a janitor that fails.
    pub fn badge_moved(&self) {
        self.badge.send_modify(|changes| *changes += 1);
    }

    /// Wakes whenever what waits for the user changed.
    pub fn badge(&self) -> tokio::sync::watch::Receiver<u64> {
        self.badge.subscribe()
    }

    /// Follows what every agent's turn shows: each time a status changes, an event with
    /// that status alone, as the stream of turns would have told it.
    pub fn statuses(&self) -> broadcast::Receiver<Arc<Value>> {
        self.statuses.subscribe()
    }

    /// Numbers an event, keeps it for those who reconnect and sends it to those who listen.
    /// The event is the object a client will read; its number is added to it here.
    pub fn publish(&self, mut event: Value, now_ms: i64) -> u64 {
        if let Some(profiles) = self.profiles.get() {
            profiles.add_to(&mut event);
        }
        // What an event about one agent's row changed, as the row has it now (issue 809).
        if event["type"] == "agent-roster"
            && let Some(directory) = self.queue_directory()
        {
            crate::roster_fields::add_to(&mut event, &directory);
        }
        let mut recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let id = recent.next_id;
        if let Some(reserved) = &self.reserved {
            reserved.cover(id);
        }
        recent.next_id += 1;
        if let Some(fields) = event.as_object_mut() {
            fields.insert("event_id".to_string(), json!(id));
        }
        let json = event.to_string();
        let published = Arc::new(Published {
            id,
            at_ms: now_ms,
            json,
        });
        while recent.events.len() >= KEEP_MAX
            || recent
                .events
                .front()
                .is_some_and(|old| now_ms - old.at_ms > KEEP_MS)
        {
            recent.events.pop_front();
        }
        recent.events.push_back(published.clone());
        // Sent while the lock is held, so that what a new listener is given from the kept
        // events and what it then hears live join up without a gap or a repeat.
        let _ = self.live.send(published);
        id
    }

    /// A live notification without a cursor or a replay record.
    pub fn ephemeral(&self, event: Value, at_ms: i64) {
        let _ = self.live.send(Arc::new(Published {
            id: 0,
            at_ms,
            json: event.to_string(),
        }));
    }

    /// The name of this run of the Host: what a `live` event and `GET /live` say under `epoch`.
    pub fn epoch(&self) -> &str {
        &self.epoch
    }

    /// Sends a `live` event to the connections that asked to be shown agents' turns. The
    /// reading of the clock in it is this moment's: a client sets its own clock by it.
    pub fn show(
        &self,
        mut event: Value,
        now_ms: i64,
        working: Option<(&str, &crate::wire::WorkingWith)>,
    ) {
        if self.statuses.receiver_count() > 0 {
            let ops = event["ops"].as_array().into_iter().flatten();
            let mut statuses = ops.filter(|op| op["op"] == "status");
            if let Some(status) = statuses.next_back() {
                let mut told = json!({
                    "type": "live", "agent_id": event["agent_id"], "session": event["session"],
                    "ops": [status],
                });
                if let Some((trace, who)) = working
                    && status["activity"]["turn_id"] == trace
                {
                    who.tell(&mut told);
                }
                let _ = self.statuses.send(Arc::new(told));
            }
        }
        if self.shown.receiver_count() == 0 {
            return;
        }
        event["server_now_ms"] = json!(now_ms);
        let whole = event.to_string();
        let of_all: Vec<Value> = event["ops"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|op| matches!(op["op"].as_str(), Some("status" | "turn")))
            .cloned()
            .collect();
        let summary = (!of_all.is_empty()).then(|| {
            event["ops"] = Value::Array(of_all);
            event.to_string()
        });
        let text = |name: &str| event[name].as_str().unwrap_or("").to_string();
        let _ = self.shown.send(Arc::new(Shown {
            session: text("session"),
            agent_id: text("agent_id"),
            whole,
            summary,
        }));
    }

    /// Starts watching what agents' turns show. Nothing from before is given.
    pub fn watch(&self) -> broadcast::Receiver<Arc<Shown>> {
        self.shown.subscribe()
    }

    /// Starts listening. Returns the kept events after `after` (all of them when the client
    /// names none), and the receiver for everything from here on.
    pub fn subscribe(
        &self,
        after: Option<u64>,
    ) -> (Vec<Arc<Published>>, broadcast::Receiver<Arc<Published>>) {
        let recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let missed = recent
            .events
            .iter()
            .filter(|event| after.is_none_or(|after| event.id > after))
            .cloned()
            .collect();
        (missed, self.live.subscribe())
    }

    /// Takes up the events after `after` (`None`: nothing is missed; the connection stands at
    /// the newest), judged under the same lock that takes them.
    pub fn resume(&self, after: Option<u64>) -> Resumed {
        let recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let head = recent.next_id.saturating_sub(1);
        let Some(after) = after else {
            return Resumed {
                missed: Vec::new(),
                live: self.live.subscribe(),
                truncated: false,
                head,
            };
        };
        let missed: Vec<Arc<Published>> = recent
            .events
            .iter()
            .filter(|event| event.id > after)
            .cloned()
            .collect();
        let oldest = recent
            .events
            .front()
            .map_or(recent.next_id, |event| event.id);
        // A number this run never gave out (an earlier run's, or one it has not reached), or
        // one older than what is kept: what came after it is not all here.
        let truncated = after < recent.first_id || after >= recent.next_id || oldest > after + 1;
        Resumed {
            missed,
            live: self.live.subscribe(),
            truncated,
            head,
        }
    }
}

/// How many event numbers a run reserves on disk at a time.
const BLOCK: u64 = 1 << 20;

/// The numbers this Host's events may have, reserved on disk before any of them is given out
/// (issue 935). The file says the end of the last reservation; a run starts at or above it, and
/// gives out no number at or above its own reservation's end before that end is on disk. So no
/// run gives out a number an earlier run may have given out, whether that run stopped, failed to
/// write at its stop, or crashed: a client resuming from an earlier run's number is always told
/// that it missed something. The next block is reserved in the background when half of this one is
/// used; a publish waits for the disk only where a block runs out before that is done.
///
/// Where the disk refuses a reservation, the guarantee lapses, and that is said once: numbers go
/// on being given out above the last reservation on disk, unreserved, so that the apps go on
/// hearing of changes, and a later run that follows a crash may give out some of them again. No
/// publish then touches the disk: the reservation is tried again in the background, at most every
/// 30 seconds, and the guarantee holds again once one is on disk above the numbers given out.
pub struct Reserved {
    path: std::path::PathBuf,
    block: u64,
    /// The end of what is on disk: no number at or above it has been given out.
    end: std::sync::atomic::AtomicU64,
    /// One write at a time.
    writing: Mutex<()>,
    /// A reservation ahead is being written.
    ahead: std::sync::atomic::AtomicBool,
    /// The disk refused a reservation: said once.
    refused: std::sync::atomic::AtomicBool,
    /// The last reservation was refused: numbers are being given out unreserved.
    lapsed: std::sync::atomic::AtomicBool,
    /// When the reservation was last tried again while lapsed.
    tried: Mutex<Option<std::time::Instant>>,
    /// How many times a reservation was written or tried: what a test of the lapse counts.
    attempts: std::sync::atomic::AtomicU64,
    /// The file is there and cannot be read: the floor is unknown, and the file is kept as it
    /// is: nothing is written over it, for the end it holds may be above anything this run knows.
    unknown: std::sync::atomic::AtomicBool,
}

/// How often a lapsed reservation is tried again, in the background.
const RETRY_LAPSED: std::time::Duration = std::time::Duration::from_secs(30);

impl Reserved {
    fn new(path: std::path::PathBuf, block: u64) -> Self {
        Self {
            path,
            block,
            end: std::sync::atomic::AtomicU64::new(0),
            writing: Mutex::new(()),
            ahead: std::sync::atomic::AtomicBool::new(false),
            refused: std::sync::atomic::AtomicBool::new(false),
            lapsed: std::sync::atomic::AtomicBool::new(false),
            tried: Mutex::new(None),
            attempts: std::sync::atomic::AtomicU64::new(0),
            unknown: std::sync::atomic::AtomicBool::new(false),
        }
    }

    /// The end of the last reservation any run made: 0 where no file is there, and an error where
    /// one is and cannot be read as a whole reservation (its digits and the newline after them, so
    /// that a file cut short is never taken for a lower end).
    fn read(&self) -> Result<u64, String> {
        let text = match std::fs::read_to_string(&self.path) {
            Ok(text) => text,
            Err(why) if why.kind() == std::io::ErrorKind::NotFound => return Ok(0),
            Err(why) => return Err(why.to_string()),
        };
        text.strip_suffix('\n')
            .filter(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|digits| digits.parse().ok())
            .ok_or_else(|| "it holds no whole reservation".to_string())
    }

    /// Makes `end` the reservation on disk, if it is not already: written beside, flushed,
    /// renamed over, and the folder flushed, so that a crash leaves the old end or the new one.
    fn reserve(&self, end: u64) -> std::io::Result<()> {
        use std::io::Write as _;
        use std::sync::atomic::Ordering;
        let _one = self.writing.lock().unwrap_or_else(PoisonError::into_inner);
        if self.end.load(Ordering::Acquire) >= end {
            return Ok(());
        }
        if self.unknown.load(Ordering::Acquire) {
            return Err(std::io::Error::other(
                "the reservation on disk could not be read, and is kept as it is",
            ));
        }
        self.attempts.fetch_add(1, Ordering::Relaxed);
        let beside = self.path.with_extension("new");
        let mut file = std::fs::File::create(&beside)?;
        file.write_all(format!("{end}\n").as_bytes())?;
        file.sync_all()?;
        std::fs::rename(&beside, &self.path)?;
        if let Some(folder) = self.path.parent() {
            std::fs::File::open(folder)?.sync_all()?;
        }
        self.end.store(end, Ordering::Release);
        Ok(())
    }

    fn refused(&self, why: &std::io::Error) {
        self.lapsed.store(true, std::sync::atomic::Ordering::Release);
        if !self.refused.swap(true, std::sync::atomic::Ordering::Relaxed) {
            eprintln!(
                "clarp-host: the numbers of its events could not be reserved on disk ({why}): a \
                 client that resumes across a crash of this run may be told it missed nothing"
            );
        }
    }

    /// The number `id` is about to be given out: it is covered by what is on disk, and the next
    /// block is on its way once half of this one is used. Lapsed, it touches no disk.
    fn cover(self: &Arc<Self>, id: u64) {
        use std::sync::atomic::Ordering;
        let end = self.end.load(Ordering::Acquire);
        if id >= end {
            if self.lapsed.load(Ordering::Acquire) {
                self.retry(id);
                return;
            }
            // Run out before the reservation ahead was on disk: this number waits for it.
            if let Err(why) = self.reserve(id.saturating_add(self.block)) {
                self.refused(&why);
            }
            return;
        }
        if id.saturating_add(self.block / 2) >= end {
            self.ahead(end.saturating_add(self.block));
        }
    }

    /// Lapsed: the reservation is tried again in the background, at most every `RETRY_LAPSED`.
    fn retry(self: &Arc<Self>, id: u64) {
        let mut tried = self.tried.lock().unwrap_or_else(PoisonError::into_inner);
        if tried.is_some_and(|at| at.elapsed() < RETRY_LAPSED) {
            return;
        }
        *tried = Some(std::time::Instant::now());
        drop(tried);
        // The block ahead of the number given out now: numbers given out meanwhile are within it.
        self.ahead(id.saturating_add(self.block));
    }

    /// Writes the reservation up to `end` on a thread of its own, one at a time.
    fn ahead(self: &Arc<Self>, end: u64) {
        use std::sync::atomic::Ordering;
        if self.ahead.swap(true, Ordering::AcqRel) {
            return;
        }
        let reserved = self.clone();
        let spawned = std::thread::Builder::new()
            .name("event-reserve".into())
            .spawn(move || {
                match reserved.reserve(end) {
                    Ok(()) => reserved.lapsed.store(false, Ordering::Release),
                    Err(why) => reserved.refused(&why),
                }
                reserved.ahead.store(false, Ordering::Release);
            });
        if spawned.is_err() {
            self.ahead.store(false, Ordering::Release);
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn roster(kind: &str) -> Value {
        json!({ "type": "agent-roster", "kind": kind })
    }

    #[test]
    fn events_are_numbered_kept_and_heard_without_a_gap() {
        let hub = Hub::new(1_700_000_000_000);
        let first = hub.publish(roster("one"), 1_700_000_000_000);
        let second = hub.publish(roster("two"), 1_700_000_000_001);
        assert_eq!(second, first + 1);

        let (missed, mut live) = hub.subscribe(Some(first));
        assert_eq!(
            missed.iter().map(|e| e.id).collect::<Vec<_>>(),
            vec![second]
        );
        let third = hub.publish(roster("three"), 1_700_000_000_002);
        let heard = live.try_recv().unwrap();
        assert_eq!(heard.id, third);
        assert!(heard.json.contains("\"type\":\"agent-roster\"") && heard.json.contains("three"));
        assert!(heard.json.contains(&format!("\"event_id\":{third}")));

        let (everything, _) = hub.subscribe(None);
        assert_eq!(
            everything.len(),
            3,
            "a client that names no event is given what is kept"
        );
    }

    #[test]
    fn no_run_gives_out_a_number_an_earlier_run_may_have_given_out_however_that_run_ended() {
        let place = std::env::temp_dir().join(format!("hub-reserved-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&place);
        std::fs::create_dir_all(&place).unwrap();
        let file = place.join("event-numbers");
        let now = 1_700_000_000_000;
        let run = |at: i64| Hub::new(at).reserving_in_blocks(file.clone(), 8);

        // Without a reservation two runs in one second of numbers give out the same numbers, and a
        // client resuming from the old run's last is told nothing was missed.
        let old = Hub::new(now);
        let b = old.publish(roster("one"), now);
        let last = old.publish(roster("two"), now + 1);
        let unreserved = Hub::new(now + 500);
        let reused: Vec<u64> = (0..3).map(|n| unreserved.publish(roster("new"), now + 500 + n)).collect();
        assert_eq!(reused, vec![b, b + 1, b + 2]);
        assert!(!unreserved.resume(Some(last)).truncated);

        // A runs past two blocks (one reserved ahead while it ran), then crashes: nothing is written
        // at its end. B starts in the same second, gives out numbers, and crashes too. C as well.
        let a = run(now);
        let a_ids: Vec<u64> = (0..20).map(|n| a.publish(roster("a"), now + n)).collect();
        drop(a);
        let b_run = run(now + 300);
        let b_ids: Vec<u64> = (0..5).map(|n| b_run.publish(roster("b"), now + 300 + n)).collect();
        assert!(b_ids[0] > *a_ids.last().unwrap(), "{b_ids:?} after {a_ids:?}");
        drop(b_run);
        let c = run(now + 600);
        let c_first = c.publish(roster("c"), now + 600);
        assert!(c_first > *b_ids.last().unwrap(), "{c_first} after {b_ids:?}");
        // A client resuming from B's last number is told it missed something, and given C's.
        let resumed = c.resume(Some(*b_ids.last().unwrap()));
        assert!(resumed.truncated);
        assert_eq!(resumed.missed.len(), 1);

        // Every number given out was below the reservation on disk when it was given out.
        let on_disk: u64 = std::fs::read_to_string(&file).unwrap().trim().parse().unwrap();
        assert!(on_disk > c_first);
        // A reservation file that is there and cannot be read is kept as it is: never written over
        // with a lower end, the run lapsed from its start, its numbers unreserved.
        for (name, kept) in [("malformed", "18829279\nnot a number\n"), ("cut", "18829")] {
            let path = place.join(name);
            std::fs::write(&path, kept).unwrap();
            let unknown = Hub::new(now).reserving_in_blocks(path.clone(), 8);
            for n in 0..20 {
                unknown.publish(roster("unknown"), now + n);
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
            assert_eq!(std::fs::read_to_string(&path).unwrap(), kept, "{name}");
            let reserved = unknown.reserved.as_ref().unwrap();
            assert!(reserved.lapsed.load(std::sync::atomic::Ordering::Acquire), "{name}");
            assert_eq!(reserved.attempts.load(std::sync::atomic::Ordering::Relaxed), 0, "{name}");
        }
        let folder = place.join("a-folder");
        std::fs::create_dir_all(&folder).unwrap();
        let unreadable = Hub::new(now).reserving_in_blocks(folder.clone(), 8);
        unreadable.publish(roster("unknown"), now);
        assert!(folder.is_dir());
        assert!(unreadable.reserved.as_ref().unwrap().unknown.load(std::sync::atomic::Ordering::Acquire));
        // A reservation that cannot be written lapses: the hub goes on giving out numbers, and no
        // publish touches the disk; it is tried again in the background, at most every 30 s.
        let unwritable = Hub::new(now).reserving_in_blocks(place.join("no/such/folder/file"), 8);
        let given: Vec<u64> = (0..100).map(|n| unwritable.publish(roster("still"), now + n)).collect();
        assert!(given.windows(2).all(|pair| pair[1] == pair[0] + 1));
        std::thread::sleep(std::time::Duration::from_millis(200));
        let reserved = unwritable.reserved.as_ref().unwrap();
        let tried = reserved.attempts.load(std::sync::atomic::Ordering::Relaxed);
        assert!(tried <= 2, "the disk was tried {tried} times for 100 events");
        assert!(reserved.lapsed.load(std::sync::atomic::Ordering::Acquire));
        std::fs::remove_dir_all(&place).unwrap();
    }

    #[test]
    fn what_a_turn_shows_goes_whole_to_those_who_follow_it_and_in_short_to_the_rest() {
        let hub = Hub::new(1_700_000_000_000);
        assert_eq!(hub.epoch(), "018bcfe56800");
        let event = |ops: Value| json!({"type": "live", "agent_id": "a1", "session": "claire", "lseq": 4, "server_now_ms": 1, "ops": ops});
        // Nobody watches: nothing is made.
        hub.show(event(json!([{"op": "status"}])), 5, None);
        let mut watching = hub.watch();
        hub.show(
            event(json!([{"op": "upsert", "id": "cl:1"}, {"op": "status", "activity": {}}, {"op": "turn"}])),
            7,
            None,
        );
        hub.show(event(json!([{"op": "append", "id": "cl:1"}])), 8, None);
        let first = watching.try_recv().unwrap();
        let wanted = |names: &[&str]| names.iter().map(|name| (*name).to_string()).collect();
        let whole: Value =
            serde_json::from_str(first.for_one(&wanted(&["claire"])).unwrap()).unwrap();
        assert_eq!(whole["ops"].as_array().unwrap().len(), 3);
        assert_eq!(
            whole["server_now_ms"], 7,
            "the clock is read when it is sent"
        );
        assert_eq!(
            first.for_one(&wanted(&["a1"])),
            first.for_one(&wanted(&["*"]))
        );
        let short: Value =
            serde_json::from_str(first.for_one(&wanted(&["mike"])).unwrap()).unwrap();
        let ops: Vec<&str> = short["ops"]
            .as_array()
            .unwrap()
            .iter()
            .map(|op| op["op"].as_str().unwrap())
            .collect();
        assert_eq!(ops, ["status", "turn"]);
        assert_eq!(short["lseq"], 4, "under the same number");
        assert_eq!(
            first.for_one(&wanted(&[])),
            first.for_one(&wanted(&["mike"]))
        );
        // An event with nothing for a list of agents is for those who follow the agent alone.
        let second = watching.try_recv().unwrap();
        assert!(second.for_one(&wanted(&["claire"])).is_some());
        assert_eq!(second.for_one(&wanted(&["mike"])), None);
        assert!(watching.try_recv().is_err());
    }

    #[test]
    fn a_resume_says_whether_the_kept_events_reach_back_to_it() {
        let hub = Hub::new(1_700_000_000_000);
        let first = hub.publish(roster("one"), 1_700_000_000_000);
        let second = hub.publish(roster("two"), 1_700_000_000_001);
        let whole = hub.resume(Some(first));
        assert_eq!(
            whole.missed.iter().map(|e| e.id).collect::<Vec<_>>(),
            vec![second]
        );
        assert!(!whole.truncated);
        assert_eq!(whole.head, second);
        assert!(!hub.resume(Some(second)).truncated, "nothing missed");
        assert!(hub.resume(Some(second)).missed.is_empty());
        // No cursor: nothing is missed, and the connection stands at the newest.
        let fresh = hub.resume(None);
        assert!(fresh.missed.is_empty() && !fresh.truncated);
        assert_eq!(fresh.head, second);
        // A number of an earlier run, or one this run never gave.
        assert!(hub.resume(Some(first - 1)).truncated);
        assert!(hub.resume(Some(second + 1)).truncated);
        // Forgotten: the run went on past what it keeps.
        hub.publish(roster("much later"), 1_700_000_000_000 + KEEP_MS + 5);
        let cut = hub.resume(Some(first));
        assert_eq!((cut.missed.len(), cut.truncated), (1, true));
        // What is heard after a resume follows what it was given, with no gap.
        let mut taken = hub.resume(Some(cut.head));
        let next = hub.publish(roster("after"), 1_700_000_000_000 + KEEP_MS + 6);
        assert_eq!(taken.live.try_recv().unwrap().id, next);
    }

    #[test]
    fn old_events_are_forgotten_and_a_later_hub_starts_above_an_earlier_one() {
        let hub = Hub::new(1_700_000_000_000);
        let old = hub.publish(roster("old"), 1_700_000_000_000);
        let new = hub.publish(roster("new"), 1_700_000_000_000 + KEEP_MS + 1);
        let (kept, _) = hub.subscribe(None);
        assert_eq!(kept.iter().map(|e| e.id).collect::<Vec<_>>(), vec![new]);
        assert!(new > old);

        let later = Hub::new(1_700_000_001_000);
        assert!(
            later.publish(roster("after a restart"), 1_700_000_001_000) > new,
            "a hub started a second later numbers above the one before it"
        );
    }
}
