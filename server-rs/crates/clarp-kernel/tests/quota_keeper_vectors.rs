//! The Quota keeper's rule, against the Python Host's own.
//!
//! `fixtures/quota-keeper-vectors.json` is written by `server-rs/tools/quota-keeper-vectors.py`:
//! the Python Host's own functions (`quota_window_key` with the backends' identities,
//! `seed_quota_window_state`, `quota_crossing`) over windows and sequences of readings around
//! the threshold, and whole stories of its keeper's lane (`AutonomyJanitors.quota_once`) on a
//! database of its own, with a reading, a clock and a push of the story's. The stories are
//! told again here with the kernel's rule, as the Host runs it: who is told, the receipts and
//! what became of them, the memory of each window, the run's line and result, and when the
//! keeper looks next.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::quota::{self, Lane, Memory, Owner};
use clarp_kernel::recovery::Accounts;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/quota-keeper-vectors.json")).unwrap()
}

#[derive(Default)]
struct Kept(BTreeMap<String, Value>);

impl Memory for Kept {
    fn get(&self, key: &str) -> Option<Value> {
        self.0.get(key).cloned()
    }
    fn set(&mut self, key: &str, kept: Value) {
        self.0.insert(key.to_string(), kept);
    }
}

#[test]
fn a_window_is_remembered_under_the_name_the_python_host_gives_it() {
    let vectors = vectors();
    let mut rounded = 0;
    for row in vectors["keys"].as_array().unwrap() {
        let provider = row["provider"].as_str().unwrap();
        let window = &row["window"];
        let account = &row["account"];
        assert_eq!(
            quota::window_key(provider, account, window),
            row["key"],
            "{row}"
        );
        assert_eq!(
            quota::old_key(provider, account, window),
            row["legacy"],
            "{row}"
        );
        if row["key"] != row["legacy"] {
            rounded += 1;
        }
    }
    // Claude's windows with a kind and a moment that says its offset; no other tool's.
    assert_eq!(
        (vectors["keys"].as_array().unwrap().len(), rounded),
        (16, 4)
    );
}

#[test]
fn a_reading_tells_the_owner_and_is_kept_as_the_python_host_says() {
    let vectors = vectors();
    let (mut readings, mut told) = (0, 0);
    for case in vectors["crossings"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let threshold = case["threshold"].as_f64().unwrap();
        let mut kept = Some(case["before"].clone()).filter(|before| !before.is_null());
        for (number, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let stamp = step["stamp"].as_i64().unwrap();
            let (written, said) = quota::crossing(kept.as_ref(), &step["used"], threshold, stamp);
            if let Some(written) = written {
                kept = Some(written);
            }
            let theirs = Some(step["kept"].clone()).filter(|kept| !kept.is_null());
            assert_eq!(kept, theirs, "{name}, reading {number}");
            assert_eq!(
                said.unwrap_or(Value::Null),
                step["told"],
                "{name}, reading {number}"
            );
            readings += 1;
            told += usize::from(!step["told"].is_null());
        }
    }
    assert_eq!((readings, told), (45, 18));
}

#[test]
fn what_was_kept_under_a_windows_old_name_is_carried_to_its_new_one() {
    for case in vectors()["seeds"].as_array().unwrap() {
        let provider = case["provider"].as_str().unwrap();
        let window = &case["window"];
        let account = json!(format!("host-1:{provider}"));
        let (key, old) = (
            quota::window_key(provider, &account, window),
            quota::old_key(provider, &account, window),
        );
        let mut kept = Kept::default();
        if case["old"] == true {
            kept.set(&old, json!({"stamp": 7, "remaining": 20}));
        }
        if case["new"] == true {
            kept.set(&key, json!({"stamp": 9, "remaining": 60}));
        }
        quota::seed(provider, &account, window, &mut kept);
        let named: BTreeMap<String, Value> = kept
            .0
            .iter()
            .map(|(name, value)| {
                let which = if *name == key { "key" } else { "legacy" };
                (which.to_string(), value.clone())
            })
            .collect();
        assert_eq!(json!(named), case["kept"], "{}", case["name"]);
    }
}

/// A receipt as the stories keep it.
#[derive(Debug, Clone)]
struct Receipt {
    id: String,
    payload: Value,
    delivery: Option<Value>,
    created_at: i64,
}

/// The keeper of a story, as the Host keeps it.
struct Story {
    agent_id: String,
    session: String,
    persona: String,
    now: i64,
    enabled: bool,
    generation: u64,
    options: Value,
    usage: Value,
    answer: Value,
    switcher: bool,
    accounts: Accounts,
    lane: Option<Lane>,
    memory: Kept,
    receipts: Vec<Receipt>,
}

/// What one tick did.
#[derive(Default)]
struct Ticked {
    pushed: Vec<Value>,
    run: Option<Value>,
    /// The push failed: the receipt stays to be delivered.
    stopped: bool,
}

impl Story {
    fn base(&self) -> (i64, i64) {
        let base = self.options["interval_seconds"].as_i64().unwrap();
        let longest = self.options["max_interval_seconds"].as_i64().unwrap();
        (base, longest)
    }

    fn lane(&self) -> Lane {
        Lane::of(self.lane.clone(), &self.agent_id, self.generation)
    }

    /// What waits to be delivered is delivered, or cancelled where the keeper was set anew.
    fn deliver(&mut self, ticked: &mut Ticked) {
        for receipt in self.receipts.iter_mut().filter(|r| r.delivery.is_none()) {
            if receipt.payload["agent_id"] != json!(self.agent_id) {
                continue;
            }
            if receipt.payload["owner_generation"] != json!(self.generation) {
                receipt.delivery = Some(json!({ "cancelled": quota::SET_ANEW }));
                continue;
            }
            ticked.pushed.push(receipt.payload.clone());
            if self.answer == "raise" {
                ticked.stopped = true;
                return;
            }
            receipt.delivery = Some(match &self.answer {
                Value::Null => json!({}),
                answer => answer.clone(),
            });
        }
    }

    fn tick(&mut self) -> Ticked {
        let mut ticked = Ticked::default();
        if !self.enabled {
            return ticked;
        }
        self.deliver(&mut ticked);
        if ticked.stopped {
            return ticked;
        }
        let mut lane = self.lane();
        let bounds = self.base();
        if self.now < lane.next_run(bounds.0) {
            return ticked;
        }
        lane.start(bounds, self.now);
        let (session, persona, agent_id) = (
            self.session.clone(),
            self.persona.clone(),
            self.agent_id.clone(),
        );
        let mode = self.options["recovery_mode"].as_str().unwrap().to_string();
        let owner = Owner {
            agent_id: &agent_id,
            session: &session,
            name: &persona,
            generation: self.generation,
            threshold: self.options["remaining_threshold"].as_f64().unwrap(),
            recovery_mode: &mode,
        };
        let notices = quota::looked(&self.usage, &owner, &mut self.memory);
        let (mut told, mut recoveries) = (0, Vec::new());
        for notice in notices {
            if self.receipts.iter().any(|r| r.id == notice.receipt_id) {
                continue;
            }
            let suppressed = self.switcher && quota::SWITCHED.contains(&notice.provider.as_str());
            let mut payload = notice.payload;
            let recovery = quota::recovery(&notice.provider, &mode, notice.spent, self.accounts);
            if let Some(recovery) = recovery {
                let status = recovery["status"].as_str().unwrap_or("unknown");
                recoveries.push(format!("{}: {status}", notice.provider));
                payload["recovery"] = recovery;
            }
            let delivery = suppressed.then(|| json!({ "cancelled": quota::SWITCHER_TELLS }));
            self.receipts.push(Receipt {
                id: notice.receipt_id,
                payload,
                delivery,
                created_at: self.now,
            });
            told += usize::from(!suppressed);
        }
        self.deliver(&mut ticked);
        if ticked.stopped {
            self.lane = Some(lane);
            return ticked;
        }
        let (activity, summary, result) = quota::ran(told, &recoveries);
        lane.record(bounds, (activity, &summary), self.now);
        self.lane = Some(lane);
        ticked.run = Some(json!({
            "status": "completed", "outcome": "completed", "activity": activity,
            "activity_summary": summary, "created_at": self.now, "finished_at": self.now,
            "generation": self.generation, "result": result,
        }));
        ticked
    }

    fn receipts(&self) -> Value {
        let mut receipts = self.receipts.clone();
        receipts.sort_by(|a, b| (a.created_at, &a.id).cmp(&(b.created_at, &b.id)));
        let receipts = receipts.iter().map(|r| {
            json!({ "receipt_id": r.id, "payload": r.payload, "delivery": r.delivery,
                    "created_at": r.created_at })
        });
        Value::Array(receipts.collect())
    }

    fn lane_shown(&self) -> Value {
        let mut lane = serde_json::to_value(self.lane()).unwrap();
        lane.as_object_mut().unwrap().remove("last_check");
        lane
    }
}

/// Where a story here goes another way than on the Python Host, and why.
const BROKE_THERE: &str = "a window dated with nothing breaks the whole look on the Python Host";

#[test]
fn the_keepers_lane_tells_keeps_and_waits_as_the_python_host_does() {
    let vectors = vectors();
    let (mut ticks, mut runs, mut pushed) = (0, 0, 0);
    for told in vectors["stories"].as_array().unwrap() {
        let name = told["name"].as_str().unwrap();
        let steps = told["steps"].as_array().unwrap();
        let mut story = Story {
            agent_id: told["agent_id"].as_str().unwrap().to_string(),
            session: told["session"].as_str().unwrap().to_string(),
            persona: told["persona"].as_str().unwrap().to_string(),
            now: steps[0]["now"].as_i64().unwrap(),
            enabled: true,
            generation: 0,
            options: Value::Null,
            usage: json!({ "providers": {} }),
            answer: json!({ "enabled": true, "sent": 1, "failed": 0, "disabled": 0 }),
            switcher: false,
            accounts: Accounts::None,
            lane: None,
            memory: Kept::default(),
            receipts: Vec::new(),
        };
        if name == BROKE_THERE {
            the_story_that_breaks_there(&mut story, steps);
            continue;
        }
        for (number, step) in steps.iter().enumerate() {
            let at = format!("{name}, step {number} ({})", step["do"]);
            story.now = step["now"].as_i64().unwrap();
            story.enabled = step["enabled"] == true;
            story.generation = step["generation"].as_u64().unwrap();
            story.options = step["options"].clone();
            match step["do"].as_str().unwrap() {
                "usage" => story.usage = json!({ "providers": step["providers"] }),
                "push" => story.answer = step["answer"].clone(),
                "switcher" => story.switcher = true,
                "selector" => {
                    story.accounts = match step["value"] {
                        Value::Null => Accounts::None,
                        _ => Accounts::Select,
                    }
                }
                "tick" => {
                    let ticked = story.tick();
                    assert_eq!(json!(ticked.pushed), step["pushed"], "{at}");
                    let theirs = step["runs"].as_array().unwrap();
                    if ticked.stopped {
                        // The push broke the Python Host's look: its run is left running. A
                        // push here says what became of it, and does not break.
                        assert_eq!(theirs.len(), 1, "{at}");
                        assert_eq!(theirs[0]["status"], "running", "{at}");
                    } else {
                        let ours: Vec<Value> = ticked.run.into_iter().collect();
                        assert_eq!(json!(ours), json!(theirs), "{at}");
                        runs += ours.len();
                    }
                    pushed += step["pushed"].as_array().unwrap().len();
                    ticks += 1;
                }
                _ => {}
            }
            assert_eq!(json!(story.memory.0), step["memory"], "{at}");
            assert_eq!(story.receipts(), step["receipts"], "{at}");
            assert_eq!(story.lane_shown(), step["lane"], "{at}");
            let next = story.lane().next_run(story.base().0);
            assert_eq!(json!(next), step["next_run_at"], "{at}");
        }
    }
    assert_eq!((ticks, runs, pushed), (23, 19, 17));
}

/// A window whose time of seeing is nothing: the Python Host breaks its look there, after
/// the windows before it, and on every look after; this Host passes the window over.
fn the_story_that_breaks_there(story: &mut Story, steps: &[Value]) {
    story.generation = steps[0]["generation"].as_u64().unwrap();
    story.options = steps[0]["options"].clone();
    story.usage = json!({ "providers": steps[0]["providers"] });
    let broke: Vec<&Value> = steps.iter().filter(|step| step["do"] == "tick").collect();
    assert!(broke.iter().all(|step| step["broke"] == "AttributeError"
        || step["runs"][0]["status"] == "running"));
    let ticked = story.tick();
    let previews: Vec<&Value> = ticked.pushed.iter().map(|p| &p["preview"]).collect();
    assert_eq!(
        previews,
        [
            &json!("codex 5h: 20% quota remaining"),
            &json!("codex 5h: 5% quota remaining")
        ]
    );
    let run = ticked.run.unwrap();
    assert_eq!(run["status"], "completed");
    assert_eq!(run["result"]["item_count"], 2);
    // There: only the window before the one with nothing, and not delivered in that look.
    let theirs = steps[1]["receipts"].as_array().unwrap();
    assert_eq!(theirs.len(), 1);
    assert_eq!(theirs[0]["delivery"], Value::Null);
}

#[test]
fn a_recovery_asked_where_none_runs_answers_as_the_python_host_does() {
    let spent = |provider, mode, accounts| quota::recovery(provider, mode, true, accounts);
    assert_eq!(spent("codex", "notify", Accounts::Select), None);
    assert_eq!(
        quota::recovery("codex", "ask", false, Accounts::Select),
        None
    );
    assert_eq!(
        spent("agy", "ask", Accounts::Select).unwrap()["status"],
        "unsupported"
    );
    for accounts in [Accounts::None, Accounts::Follow] {
        assert_eq!(
            spent("claude", "ask", accounts).unwrap()["status"],
            "unconfigured"
        );
    }
    let select = spent("codex", "automatic", Accounts::Select).unwrap();
    assert_eq!(select, json!({ "status": "runtime_unavailable" }));
}
