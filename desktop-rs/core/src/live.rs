//! Live items (docs/live-items.md): a conversation's open turn as items
//! pushed as ops. [`LiveView`] is the client reducer, kept in step with the
//! Host's reference reducer (`server/lib/live_items.py`) by the recorded
//! streams in `contract/live/`.

use std::collections::{HashMap, HashSet};

use serde_json::{Value, json};

use crate::json::Object;

/// The effect asking the client to fetch `GET /live?session=`.
pub const FETCH_LIVE: &str = "fetch_live";
/// `tool.output.tail` holds at most this many lines.
pub const OUTPUT_TAIL_LINES: usize = 50;

pub fn is_terminal(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "interrupted")
}

fn idle() -> Object {
    json!({"state": "idle"}).as_object().cloned().unwrap_or_default()
}

/// Fields absent stay, `null` clears, objects merge, other values replace.
pub fn merge_patch(target: &mut Object, patch: &Object) {
    for (key, value) in patch {
        match (value, target.get_mut(key)) {
            (Value::Object(patch), Some(Value::Object(existing))) => merge_patch(existing, patch),
            _ => {
                target.insert(key.clone(), value.clone());
            }
        }
    }
}

fn append_output(item: &mut Object, lines: &[Value], total_lines: i64) {
    let tool = item.entry("tool").or_insert_with(|| json!({}));
    if !tool.is_object() {
        *tool = json!({});
    }
    let tool = tool.as_object_mut().expect("an object");
    let output = tool.entry("output").or_insert(Value::Null);
    if !output.is_object() {
        *output = json!({"tail": [], "total_lines": 0, "truncated": false, "exit_code": null});
    }
    let output = output.as_object_mut().expect("an object");
    let mut tail: Vec<Value> = output.get("tail").and_then(Value::as_array).cloned().unwrap_or_default();
    tail.extend(lines.iter().cloned());
    let keep = tail.len().saturating_sub(OUTPUT_TAIL_LINES);
    tail.drain(..keep);
    let held = tail.len() as i64;
    output.insert("tail".into(), Value::Array(tail));
    output.insert("total_lines".into(), json!(total_lines));
    output.insert("truncated".into(), json!(total_lines > held));
}

/// Whether `turn` is one for other agents (`audience` `agents`, Host
/// contract 108); a turn without an audience is the user's.
pub fn for_agents(turn: Option<&Object>) -> bool {
    turn.and_then(|t| t.get("audience")).and_then(Value::as_str).is_some_and(|a| a.trim().eq_ignore_ascii_case("agents"))
}

fn turn_id(turn: Option<&Object>) -> &str {
    turn.and_then(|t| t.get("turn_id")).and_then(Value::as_str).unwrap_or_default()
}

/// What a turn wrote for other agents (docs/protocol.md "Live rows follow
/// the same rule"; docs/live-items.md §1.4). A turn for other agents the
/// user never saw is all theirs. Once the user has seen a turn, what was
/// shown stays shown, also when the turn goes back to other agents
/// (`turn_audience_returns`, Rust Host contract 164): only the items that
/// begin while it is theirs again are theirs, and they stay theirs when the
/// user steps in once more, as their `/log` rows stay `agents`.
#[derive(Debug, Clone, Default)]
struct Veil {
    /// Turns the user has seen (`audience` `user`), newest last.
    seen: Vec<String>,
    /// Items that began in a seen turn while it was for other agents.
    items: HashSet<String>,
}

/// How many seen turns a view remembers (more than it retires).
const SEEN_TURNS: usize = 64;

impl Veil {
    fn seen(&self, turn: &str) -> bool {
        !turn.is_empty() && self.seen.iter().any(|t| t == turn)
    }
    /// The turn as it stands now.
    fn follow(&mut self, turn: Option<&Object>) {
        let id = turn_id(turn);
        if turn.is_none() || id.is_empty() || for_agents(turn) || self.seen(id) {
            return;
        }
        self.seen.push(id.to_owned());
        if self.seen.len() > SEEN_TURNS {
            self.seen.remove(0);
        }
    }
    /// Item `id` began while the turn stood as `turn`.
    fn begin(&mut self, turn: Option<&Object>, id: &str) {
        if for_agents(turn) && self.seen(turn_id(turn)) {
            self.items.insert(id.to_owned());
        }
    }
}

/// One conversation's live state, built from snapshots and events.
#[derive(Debug, Clone)]
pub struct LiveView {
    epoch: Option<String>,
    lseq: Option<i64>,
    activity: Object,
    turn: Option<Object>,
    /// The agent's own turn while a conversation fork's turn is `turn`
    /// (`conversation_fork`, contract 153): `{turn_id, audience, status,
    /// started_at_ms, tool_count, activity}`; None when absent or null.
    main_turn: Option<Object>,
    items: HashMap<String, Object>,
    awaiting_snapshot: bool,
    /// Events that came while a `GET /live` was outstanding: those above
    /// the snapshot's lseq apply once it lands. The snapshot may have been
    /// taken before they happened, and nothing else may follow them.
    held_back: Vec<Object>,
    tool_explanations: Option<Object>,
    /// Host clock minus this machine's, from the last `server_now_ms`.
    clock_offset_ms: i64,
    /// Bumped on every applied snapshot or event (what the views compare).
    generation: u64,
    /// Turns a newer one replaced, with their items (oldest first): history
    /// folds them as they read live.
    retired: Vec<(Object, Vec<Object>)>,
    veil: Veil,
}

/// How many replaced turns a view keeps.
const RETIRED_TURNS: usize = 32;
/// How many events a view keeps while a snapshot is outstanding; past
/// that the oldest go, and the gap they leave asks for a snapshot again.
const HELD_BACK: usize = 512;

impl Default for LiveView {
    fn default() -> Self {
        Self {
            epoch: None,
            lseq: None,
            activity: idle(),
            turn: None,
            main_turn: None,
            items: HashMap::new(),
            awaiting_snapshot: false,
            held_back: Vec::new(),
            tool_explanations: None,
            clock_offset_ms: 0,
            generation: 0,
            retired: Vec::new(),
            veil: Veil::default(),
        }
    }
}

impl LiveView {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn epoch(&self) -> Option<&str> {
        self.epoch.as_deref()
    }
    pub fn lseq(&self) -> Option<i64> {
        self.lseq
    }
    pub fn activity(&self) -> &Object {
        &self.activity
    }
    pub fn turn(&self) -> Option<&Object> {
        self.turn.as_ref()
    }
    /// The agent's own turn beside a conversation fork's (`main_turn`).
    pub fn main_turn(&self) -> Option<&Object> {
        self.main_turn.as_ref()
    }
    /// A `GET /live` is outstanding: events wait until it lands.
    pub fn awaiting_snapshot(&self) -> bool {
        self.awaiting_snapshot
    }
    /// The Host's tool-explanation setting, as the last snapshot said.
    pub fn tool_explanations(&self) -> Option<&Object> {
        self.tool_explanations.as_ref()
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    /// Every item, sorted by `ordinal` (ties by id, so the order is stable).
    pub fn items(&self) -> Vec<&Object> {
        let mut items: Vec<&Object> = self.items.values().collect();
        items.sort_by(|a, b| {
            let ordinal = |i: &Object| i.get("ordinal").and_then(Value::as_i64).unwrap_or(0);
            let id = |i: &Object| i.get("id").and_then(Value::as_str).unwrap_or_default().to_owned();
            ordinal(a).cmp(&ordinal(b)).then_with(|| id(a).cmp(&id(b)))
        });
        items
    }
    pub fn item(&self, id: &str) -> Option<&Object> {
        self.items.get(id)
    }
    /// A turn a newer one replaced and its items, sorted by `ordinal`.
    pub fn retired(&self, turn_id: &str) -> Option<(&Object, Vec<&Object>)> {
        let (turn, items) = self.retired.iter().rev().find(|(turn, _)| turn.get("turn_id").and_then(Value::as_str) == Some(turn_id))?;
        Some((turn, items.iter().collect()))
    }

    /// Whether `item` of `turn` (the open turn, or one it retired) was
    /// written for other agents: a chat that hides coordination between
    /// agents leaves it out ([`Veil`]).
    pub fn veiled(&self, turn: &Object, item: &Object) -> bool {
        let id = item.get("id").and_then(Value::as_str).unwrap_or_default();
        self.veil.items.contains(id) || (for_agents(Some(turn)) && !self.veil.seen(turn_id(Some(turn))))
    }

    /// Forgets veiled items neither the open turn nor a retired one holds.
    fn prune_veil(&mut self) {
        if self.veil.items.is_empty() {
            return;
        }
        let held: HashSet<&str> = self.items.keys().map(String::as_str)
            .chain(self.retired.iter().flat_map(|(_, items)| items.iter().filter_map(|i| i.get("id").and_then(Value::as_str))))
            .collect();
        self.veil.items.retain(|id| held.contains(id.as_str()));
    }

    /// Retired live turns remain available until /log has caught up.
    pub fn retired_turns(&self) -> impl Iterator<Item = (&Object, &[Object])> {
        self.retired.iter().map(|(turn, items)| (turn, items.as_slice()))
    }

    /// Keeps the held turn and its items when `next` is another turn.
    fn retire(&mut self, next: Option<&Object>) {
        let id = |t: &Object| t.get("turn_id").and_then(Value::as_str).map(str::to_owned);
        let Some(held) = self.turn.as_ref() else { return };
        if id(held).is_none() || next.is_none_or(|n| id(n) == id(held)) {
            return;
        }
        let items = self.items().into_iter().cloned().collect();
        self.retired.retain(|(turn, _)| id(turn) != id(held));
        self.retired.push((held.clone(), items));
        if self.retired.len() > RETIRED_TURNS {
            self.retired.remove(0);
        }
    }

    /// Host time now, by this machine's clock corrected with the last
    /// `server_now_ms` (what a running item's elapsed time ticks from).
    pub fn host_now_ms(&self, local_now_ms: i64) -> i64 {
        local_now_ms + self.clock_offset_ms
    }
    /// Records the Host clock an event or snapshot carried.
    pub fn observe_clock(&mut self, server_now_ms: i64, local_now_ms: i64) {
        if server_now_ms > 0 {
            self.clock_offset_ms = server_now_ms - local_now_ms;
        }
    }

    /// The client asked for `GET /live` on its own (opening a chat): events
    /// wait for it as they do after a gap.
    pub fn expect_snapshot(&mut self) {
        self.awaiting_snapshot = true;
    }
    /// The fetch failed: the next event may ask again.
    pub fn snapshot_failed(&mut self) {
        self.awaiting_snapshot = false;
        self.held_back.clear();
    }

    /// Replaces the state with a `GET /live` answer and holds its lseq,
    /// then applies the events that came while it was outstanding and are
    /// newer than it; returns the effects those ask for ([`FETCH_LIVE`]).
    pub fn apply_snapshot(&mut self, snapshot: &Object) -> Vec<&'static str> {
        // A snapshot of this epoch below the lseq held was taken before
        // events this view already applied (two fetches crossed): it would
        // put the view back, so only what waited for it goes on.
        let epoch = snapshot.get("epoch").and_then(Value::as_str);
        let stale = epoch.is_some() && epoch == self.epoch.as_deref()
            && snapshot.get("lseq").and_then(Value::as_i64).zip(self.lseq).is_some_and(|(lseq, held)| lseq < held);
        if stale {
            self.awaiting_snapshot = false;
            return self.apply_held_back();
        }
        self.epoch = snapshot.get("epoch").and_then(Value::as_str).map(str::to_owned);
        self.lseq = snapshot.get("lseq").and_then(Value::as_i64);
        self.activity = snapshot.get("activity").and_then(Value::as_object).cloned().unwrap_or_else(idle);
        let next = snapshot.get("turn").and_then(Value::as_object);
        self.retire(next);
        // Items of the same turn this view did not hold began while it
        // stood as the snapshot says.
        let same = turn_id(self.turn.as_ref()) == turn_id(next);
        let known: HashSet<String> = if same { self.items.keys().cloned().collect() } else { HashSet::new() };
        self.turn = next.cloned();
        self.veil.follow(self.turn.as_ref());
        self.main_turn = snapshot.get("main_turn").and_then(Value::as_object).cloned();
        self.items = snapshot
            .get("items")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_object)
            .filter_map(|item| Some((item.get("id")?.as_str()?.to_owned(), item.clone())))
            .collect();
        let fresh: Vec<String> = self.items.keys().filter(|id| !known.contains(*id)).cloned().collect();
        for id in fresh {
            self.veil.begin(self.turn.as_ref(), &id);
        }
        self.prune_veil();
        if let Some(settings) = snapshot.get("tool_explanations").and_then(Value::as_object) {
            self.tool_explanations = Some(settings.clone());
        }
        self.awaiting_snapshot = false;
        self.generation += 1;
        self.apply_held_back()
    }

    /// The events that waited for a snapshot, applied by §3's rules: one
    /// that leaves a gap asks again, and the rest wait for that one.
    fn apply_held_back(&mut self) -> Vec<&'static str> {
        let mut effects = Vec::new();
        for event in std::mem::take(&mut self.held_back) {
            effects.extend(self.apply_event(&event));
        }
        effects
    }

    /// Applies one `live` event by §3's rules; returns the effects the
    /// client must run ([`FETCH_LIVE`]). An event is applied whole or not
    /// at all.
    pub fn apply_event(&mut self, event: &Object) -> Vec<&'static str> {
        if self.awaiting_snapshot {
            if self.held_back.len() >= HELD_BACK {
                self.held_back.remove(0);
            }
            self.held_back.push(event.clone());
            return Vec::new();
        }
        let lseq = event.get("lseq").and_then(Value::as_i64).unwrap_or(0);
        let Some(held) = self.lseq else { return self.need_snapshot() };
        if event.get("epoch").and_then(Value::as_str) != self.epoch.as_deref() {
            return self.need_snapshot();
        }
        if lseq <= held {
            return Vec::new();
        }
        if lseq != held + 1 {
            return self.need_snapshot();
        }
        let mut staged = (self.activity.clone(), self.turn.clone(), self.items.clone());
        let mut main_turn = self.main_turn.clone();
        let mut veil = self.veil.clone();
        for op in event.get("ops").and_then(Value::as_array).into_iter().flatten() {
            let Some(op) = op.as_object() else { continue };
            let name = op.get("op").and_then(Value::as_str).unwrap_or_default();
            // The agent's own turn beside a fork's: replaced whole (null: gone).
            if name == "main_turn" {
                main_turn = op.get("main_turn").and_then(Value::as_object).cloned();
                continue;
            }
            let id = op.get("id").and_then(Value::as_str).unwrap_or_default();
            let begins = name == "upsert" && !id.is_empty() && !staged.2.contains_key(id);
            if !apply_op(&mut staged, op) {
                return self.need_snapshot();
            }
            if name == "turn" {
                veil.follow(staged.1.as_ref());
            } else if begins {
                veil.begin(staged.1.as_ref(), id);
            }
        }
        self.retire(staged.1.as_ref());
        (self.activity, self.turn, self.items) = staged;
        self.veil = veil;
        self.prune_veil();
        self.main_turn = main_turn;
        self.lseq = Some(lseq);
        self.generation += 1;
        Vec::new()
    }

    fn need_snapshot(&mut self) -> Vec<&'static str> {
        self.awaiting_snapshot = true;
        vec![FETCH_LIVE]
    }
}

type Staged = (Object, Option<Object>, HashMap<String, Object>);

/// One op; false when it shows something was missed (a revision out of
/// step, a patch for an unknown item).
fn apply_op((activity, turn, items): &mut Staged, op: &Object) -> bool {
    let name = op.get("op").and_then(Value::as_str).unwrap_or_default();
    match name {
        "status" => {
            *activity = op.get("activity").and_then(Value::as_object).cloned().unwrap_or_else(idle);
            return true;
        }
        "turn" => {
            let next = op.get("turn").and_then(Value::as_object).cloned();
            let id = |t: &Object| t.get("turn_id").cloned();
            // A new turn: the last one's items now live in /log.
            if let (Some(held), Some(new)) = (turn.as_ref(), next.as_ref())
                && id(held) != id(new)
            {
                items.clear();
            }
            *turn = next;
            return true;
        }
        "upsert" | "append" | "done" => {}
        // Unknown ops are ignored.
        _ => return true,
    }
    let Some(id) = op.get("id").and_then(Value::as_str) else { return true };
    let rev = op.get("rev").and_then(Value::as_i64);
    let item = match items.get_mut(id) {
        Some(item) => {
            let held = item.get("rev").and_then(Value::as_i64).unwrap_or(0);
            if rev != Some(held + 1) {
                return false;
            }
            item
        }
        None if name != "upsert" => return false,
        None => {
            let kind = op.get("kind").cloned().unwrap_or(Value::Null);
            items.insert(id.to_owned(), json!({"id": id, "kind": kind, "rev": 0}).as_object().cloned().unwrap_or_default());
            items.get_mut(id).expect("just inserted")
        }
    };
    let before = item.get("status").cloned().unwrap_or(Value::Null);
    let settled = is_terminal(before.as_str().unwrap_or_default());
    let patch = op.get("item").and_then(Value::as_object);
    match name {
        "upsert" => {
            if let Some(patch) = patch {
                merge_patch(item, patch);
            }
        }
        "append" => match op.get("field").and_then(Value::as_str) {
            Some("text") => {
                let mut text = item.get("text").and_then(Value::as_str).unwrap_or_default().to_owned();
                text.push_str(op.get("chunk").and_then(Value::as_str).unwrap_or_default());
                item.insert("text".into(), json!(text));
            }
            Some("tool.output") => {
                let lines = op.get("lines").and_then(Value::as_array).cloned().unwrap_or_default();
                append_output(item, &lines, op.get("total_lines").and_then(Value::as_i64).unwrap_or(0));
            }
            _ => {}
        },
        _ => {
            item.insert("status".into(), op.get("status").cloned().unwrap_or(Value::Null));
            for key in ["started_at_ms", "ended_at_ms"] {
                if let Some(value) = op.get(key) {
                    item.insert(key.into(), value.clone());
                }
            }
            if let Some(patch) = patch {
                merge_patch(item, patch);
            }
        }
    }
    // Terminal statuses are final: nothing moves a settled item back.
    if settled && !is_terminal(item.get("status").and_then(Value::as_str).unwrap_or_default()) {
        item.insert("status".into(), before);
    }
    item.insert("rev".into(), json!(rev.unwrap_or_else(|| item.get("rev").and_then(Value::as_i64).unwrap_or(0) + 1)));
    true
}
