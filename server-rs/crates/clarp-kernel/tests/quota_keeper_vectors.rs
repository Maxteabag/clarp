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

use clarp_kernel::janitors::quota::{self, Deliver, Lane, Memory, Owner};
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
    /// The owner has a selector for the tools (`--accounts select` with one configured).
    selector: bool,
    /// The keeper has a runtime to recover with: the story's reference was given one.
    runtime: bool,
    /// The turns of the tools' pools, as the coordinator stood in: agent, trace, still its
    /// agent's; and whether the pool takes a recovery.
    attempts: Vec<(String, String, bool)>,
    accepts: bool,
    /// The cards in the keeper's session, as the Host shows them (`payload`, `decision`).
    cards: Vec<Value>,
    /// The cards a recovery ran by.
    approvals: Vec<String>,
    lane: Option<Lane>,
    memory: Kept,
    receipts: Vec<Receipt>,
}

/// What one tick did.
#[derive(Default)]
struct Ticked {
    pushed: Vec<Value>,
    /// What the pools were asked.
    asked: Vec<Value>,
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

    /// What waits to be delivered, as the kernel says: pushed, settled without a push, or left.
    fn deliver(&mut self, ticked: &mut Ticked) {
        let waiting: Vec<(String, i64, Value)> = self
            .receipts
            .iter()
            .filter(|r| r.delivery.is_none())
            .map(|r| (r.id.clone(), r.created_at, r.payload.clone()))
            .collect();
        let waiting: Vec<quota::Waiting<'_>> = waiting
            .iter()
            .map(|(id, at, payload)| (id.as_str(), *at, payload))
            .collect();
        let interval = self.options["interval_seconds"].as_i64().unwrap();
        let owner = (self.agent_id.as_str(), self.generation);
        for (id, action) in quota::delivering(&waiting, owner, interval, self.now) {
            let receipt = self.receipts.iter_mut().find(|r| r.id == id).unwrap();
            match action {
                Deliver::Wait => {}
                Deliver::Settle(settled) => receipt.delivery = Some(settled),
                Deliver::Push => {
                    ticked.pushed.push(quota::pushed_of(&receipt.payload));
                    if self.answer == "raise" {
                        ticked.stopped = true;
                        return;
                    }
                    let told =
                        quota::after_push(&mut receipt.payload, self.answer.clone(), self.now);
                    receipt.delivery = told;
                }
            }
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
        if self.runtime {
            let cards = self.cards.clone();
            let acted = self.approvals.clone();
            let approvals = quota::approved(&cards, (&agent_id, self.generation), |id| {
                acted
                    .iter()
                    .any(|approval| approval == &format!("quota-keeper.approval.{id}"))
            });
            for (id, provider) in approvals {
                let card = cards.iter().find(|card| card["artifact_id"] == id);
                let status = self.recover(provider, &mode, card, &mut ticked);
                if quota::settled(&status) {
                    self.approvals.push(format!("quota-keeper.approval.{id}"));
                }
            }
        }
        let notices = quota::looked(&self.usage, &owner, &mut self.memory);
        let (mut told, mut recoveries) = (0, Vec::new());
        for notice in notices {
            if self.receipts.iter().any(|r| r.id == notice.receipt_id) {
                continue;
            }
            let suppressed = self.switcher && quota::SWITCHED.contains(&notice.provider.as_str());
            let mut payload = notice.payload;
            let provider = notice.provider.clone();
            let asked = quota::asked(&provider, &mode, notice.spent, self.selector);
            let recovery = match asked {
                None => None,
                // The Python Host's keeper with no runtime to recover with.
                Some(quota::Asked::Recover) if !self.runtime => {
                    Some(json!({ "status": "runtime_unavailable" }))
                }
                Some(quota::Asked::Recover) => {
                    Some(json!({ "status": self.recover(&provider, &mode, None, &mut ticked) }))
                }
                Some(quota::Asked::Card) => {
                    let mut card = quota::card(&provider, &owner, &notice.receipt_id);
                    let id = format!("card-{}", self.cards.len() + 1);
                    card["artifact_id"] = json!(id);
                    card["decision"] = json!({ "status": "pending", "resolved_choice": "" });
                    self.cards.push(card);
                    Some(json!({ "status": "awaiting_approval", "artifact_id": id }))
                }
                Some(refused) => quota::refused(refused),
            };
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
            // The push raised (the reference's push; this Host's never does): the look's run is
            // closed as failed, what it read before kept, and its lane told (R100).
            let why = quota::stopped("RuntimeError");
            lane.record(bounds, ("failed", &why), self.now);
            self.lane = Some(lane);
            ticked.run = Some(json!({
                "status": "failed", "outcome": "failed", "activity": "failed",
                "activity_summary": why, "created_at": self.now, "finished_at": self.now,
                "generation": self.generation, "result": {},
            }));
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

    /// A recovery run now, for this tool, by this card where the owner approved one: what
    /// the runtime and the pool answer, the pool's request kept as what it was asked.
    fn recover(
        &mut self,
        provider: &str,
        mode: &str,
        card: Option<&Value>,
        ticked: &mut Ticked,
    ) -> String {
        let standing = quota::Standing {
            enabled: self.enabled,
            job: quota::ROLE,
            generation: self.generation,
            mode,
        };
        let frozen = (self.agent_id.as_str(), self.generation, provider);
        if let Err(said) = quota::may_recover(Some(standing), frozen, card, self.selector) {
            return said.to_string();
        }
        let first = self.attempts.iter().find(|(_, _, owned)| *owned).cloned();
        let taken = first.map(|(agent, trace, _)| {
            ticked.asked.push(json!({
                "provider": provider, "agent_id": agent, "trace_id": trace, "command": "cycler",
            }));
            self.accepts
        });
        quota::recovered(taken).to_string()
    }

    /// The cards as the Python Host's database holds them.
    fn cards_shown(&self) -> Value {
        let shown = self.cards.iter().map(|card| {
            json!({
                "artifact_id": card["artifact_id"], "title": card["title"],
                "question": card["question"], "context": card["context"],
                "reference_id": card["reference_id"], "payload": card["payload"],
                "status": card["decision"]["status"],
                "resolved_choice": card["decision"]["resolved_choice"],
            })
        });
        Value::Array(shown.collect())
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

#[test]
fn the_keepers_lane_tells_keeps_and_waits_as_the_python_host_does() {
    let vectors = vectors();
    let (mut ticks, mut runs, mut pushed, mut asked) = (0, 0, 0, 0);
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
            selector: false,
            runtime: told["steps"][0].get("cards").is_some(),
            attempts: Vec::new(),
            accepts: true,
            cards: Vec::new(),
            approvals: Vec::new(),
            lane: None,
            memory: Kept::default(),
            receipts: Vec::new(),
        };
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
                "selector" => story.selector = !step["value"].is_null(),
                "coordinator" => {
                    let attempts = step["attempts"].as_array().unwrap().iter().map(|row| {
                        let text = |key: &str| row[key].as_str().unwrap().to_string();
                        (text("agent_id"), text("trace_id"), row["owned"] == true)
                    });
                    story.attempts = attempts.collect();
                    story.accepts = step["accepts"] == true;
                }
                "resolve" => {
                    let card = story.cards.last_mut().unwrap();
                    card["decision"] =
                        json!({ "status": step["choice"], "resolved_choice": step["choice"] });
                }
                "tick" => {
                    let ticked = story.tick();
                    assert_eq!(json!(ticked.pushed), step["pushed"], "{at}");
                    if story.runtime {
                        assert_eq!(json!(ticked.asked), step["asked"], "{at}");
                        asked += ticked.asked.len();
                    }
                    let theirs = step["runs"].as_array().unwrap();
                    let ours: Vec<Value> = ticked.run.into_iter().collect();
                    assert_eq!(json!(ours), json!(theirs), "{at}");
                    runs += ours.len();
                    pushed += step["pushed"].as_array().unwrap().len();
                    ticks += 1;
                }
                _ => {}
            }
            if story.runtime {
                assert_eq!(story.cards_shown(), step["cards"], "{at}");
                assert_eq!(json!(story.approvals), step["approvals"], "{at}");
            }
            assert_eq!(json!(story.memory.0), step["memory"], "{at}");
            assert_eq!(story.receipts(), step["receipts"], "{at}");
            assert_eq!(story.lane_shown(), step["lane"], "{at}");
            let next = story.lane().next_run(story.base().0);
            assert_eq!(json!(next), step["next_run_at"], "{at}");
        }
    }
    assert_eq!((ticks, runs, pushed, asked), (46, 43, 34, 4));
}

#[test]
fn a_recovery_is_asked_and_run_as_the_python_host_asks_and_runs_it() {
    use quota::Asked;
    // How it is asked: of nothing where the keeper only tells or something is left.
    assert_eq!(quota::asked("codex", "notify", true, true), None);
    assert_eq!(quota::asked("codex", "ask", false, true), None);
    assert_eq!(
        quota::asked("agy", "ask", true, true),
        Some(Asked::Unsupported)
    );
    assert_eq!(
        quota::asked("claude", "automatic", true, false),
        Some(Asked::Unconfigured)
    );
    assert_eq!(quota::asked("codex", "ask", true, true), Some(Asked::Card));
    assert_eq!(
        quota::asked("codex", "automatic", true, true),
        Some(Asked::Recover)
    );
    assert_eq!(
        quota::refused(Asked::Unconfigured).unwrap()["status"],
        "unconfigured"
    );
    assert_eq!(quota::refused(Asked::Card), None);
    // Whether it may run: the runtime's own checks, in their order.
    let standing = |enabled, generation, mode| quota::Standing {
        enabled,
        job: quota::ROLE,
        generation,
        mode,
    };
    let card = |choice: &str, generation: u64| {
        json!({
            "decision": { "resolved_choice": choice },
            "payload": { "quota_recovery": { "owner_id": "k", "generation": generation, "provider": "codex" } },
        })
    };
    let may = |now, card: Option<&Value>, selector| {
        quota::may_recover(now, ("k", 2, "codex"), card, selector)
    };
    assert_eq!(may(None, None, true), Err("cancelled"));
    assert_eq!(
        may(Some(standing(false, 2, "automatic")), None, true),
        Err("cancelled")
    );
    assert_eq!(
        may(Some(standing(true, 3, "automatic")), None, true),
        Err("cancelled")
    );
    assert_eq!(
        may(Some(standing(true, 2, "ask")), None, true),
        Err("approval_required")
    );
    assert_eq!(
        may(
            Some(standing(true, 2, "ask")),
            Some(&card("rejected", 2)),
            true
        ),
        Err("approval_required")
    );
    assert_eq!(
        may(
            Some(standing(true, 2, "ask")),
            Some(&card("accepted", 1)),
            true
        ),
        Err("approval_required")
    );
    assert_eq!(
        may(
            Some(standing(true, 2, "ask")),
            Some(&card("accepted", 2)),
            true
        ),
        Ok(())
    );
    assert_eq!(
        may(Some(standing(true, 2, "notify")), None, true),
        Err("not_authorized")
    );
    assert_eq!(
        may(Some(standing(true, 2, "automatic")), None, false),
        Err("unconfigured")
    );
    assert_eq!(
        may(Some(standing(true, 2, "automatic")), None, true),
        Ok(())
    );
    assert_eq!(quota::recovered(None), "nothing_to_resume");
    assert!(
        quota::settled("recovering")
            && quota::settled("nothing_to_resume")
            && !quota::settled("waiting")
    );
}
