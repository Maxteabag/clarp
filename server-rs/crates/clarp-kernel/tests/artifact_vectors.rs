//! Artifacts and decisions against the Python Host's.
//!
//! `fixtures/artifact-vectors.json` is written by `server-rs/tools/artifact-vectors.py`:
//! sequences of calls of `server/lib/artifacts.py` with a fixed clock and fixed ids, each with
//! what it answered or refused. Here the same calls are made of this Host's books, driven
//! the way the Host drives them, and must come to the same.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;

use clarp_kernel::artifacts::decisions::{self, Ask, Given, OnBehalf};
use clarp_kernel::artifacts::forms;
use clarp_kernel::artifacts::reports::{self, Revisions};
use clarp_kernel::artifacts::updates;
use clarp_kernel::artifacts::{
    self, Artifact, ArtifactBook, Ctx, Listing, Made, Maker, New, Refused, Shelf, Why,
};
use clarp_kernel::goals::{Change, GoalBook, Owner, World, item_key};
use clarp_kernel::media::{self, Asset, MediaBook};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// Where two values differ, as paths into them.
fn differences(path: &str, ours: &Value, theirs: &Value, found: &mut Vec<String>) {
    match (ours, theirs) {
        (Value::Object(a), Value::Object(b)) => {
            for (key, value) in a {
                match b.get(key) {
                    Some(other) => differences(&format!("{path}.{key}"), value, other, found),
                    None => found.push(format!("{path}.{key}: only here: {value}")),
                }
            }
            for (key, value) in b {
                if !a.contains_key(key) {
                    found.push(format!("{path}.{key}: only in the Python Host's: {value}"));
                }
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (index, (value, other)) in a.iter().zip(b).enumerate() {
                differences(&format!("{path}[{index}]"), value, other, found);
            }
        }
        _ if ours == theirs => {}
        _ => found.push(format!("{path}: here {ours}, the Python Host's {theirs}")),
    }
}

struct Agent {
    agent_id: String,
    session: String,
    persona: String,
    artifacts: ArtifactBook,
    goals: GoalBook,
    /// The files it published.
    media: MediaBook,
}

impl Agent {
    fn maker(&self) -> Maker<'_> {
        Maker {
            agent_id: &self.agent_id,
            session: &self.session,
            persona: &self.persona,
        }
    }

    fn keep(&mut self, artifact: &Artifact, now: i64) {
        let record = ArtifactBook::record(artifact, now);
        let read = serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
        assert_eq!(record, read, "a record is read as it was written");
        self.artifacts.apply(&read);
    }
}

/// The agents of a scenario, driven the way the Host drives them.
struct Fleet {
    agents: Vec<Agent>,
    ids: u64,
    /// How many files were published: the Python Host's ids in the vectors are counted.
    files: u64,
    /// The pages of reports, kept beside the books under their names, as the Host keeps
    /// them beside the log.
    pages: HashMap<String, String>,
    /// The cursors pages of Updates ended with, by the names a scenario keeps them under.
    cursors: HashMap<String, String>,
}

type Said = Result<Value, Refused>;

fn refused(why: Why, message: &str) -> Said {
    Err(Refused {
        why,
        message: message.to_string(),
    })
}

fn text(args: &Value, name: &str) -> String {
    args[name].as_str().unwrap_or("").to_string()
}

impl Fleet {
    fn token(ids: &mut u64, bytes: usize) -> String {
        *ids += 1;
        format!("{:0width$x}", *ids, width = bytes * 2)
    }

    fn by_session(&self, session: &str) -> Option<usize> {
        self.agents
            .iter()
            .position(|agent| agent.session == session.trim())
    }

    fn holding(&self, artifact_id: &str) -> Option<usize> {
        self.agents
            .iter()
            .position(|agent| agent.artifacts.any(artifact_id).is_some())
    }

    fn asked(&self, decision_id: &str) -> Option<usize> {
        self.agents
            .iter()
            .position(|agent| agent.artifacts.of_decision(decision_id).is_some())
    }

    fn of_plan(&self, plan_id: &str) -> Option<usize> {
        self.agents
            .iter()
            .position(|agent| agent.goals.plan(plan_id).is_some())
    }

    fn shelves(&self) -> Vec<Shelf<'_>> {
        self.agents
            .iter()
            .map(|agent| Shelf {
                maker: agent.maker(),
                artifacts: &agent.artifacts,
                goals: &agent.goals,
            })
            .collect()
    }

    fn shown(&self, at: usize, artifact: &Artifact, now: i64) -> Value {
        let agent = &self.agents[at];
        self.answered(artifact.public(&agent.persona, &agent.goals, now))
    }

    /// A row as the Host answers it: with its report's page put back.
    fn answered(&self, mut row: Value) -> Value {
        if let Some(page) = reports::named_page(&mut row) {
            reports::with_page(&mut row, &self.pages[&page.blob()]);
        }
        row
    }

    /// Keeps what was made or changed, its page first, and shows it.
    fn kept(&mut self, at: usize, made: &Made, now: i64) -> Value {
        if let (Some(content), Some(page)) = (&made.page, &made.artifact.page) {
            self.pages.insert(page.blob(), content.clone());
        }
        if made.changed {
            self.agents[at].keep(&made.artifact, now);
        }
        self.shown(at, &made.artifact, now)
    }

    /// Makes an artifact for a session, with the ids the whole fleet counts.
    fn make(
        &mut self,
        session: &str,
        now: i64,
        nobody: impl FnOnce() -> Refused,
        make: impl FnOnce(&ArtifactBook, Maker<'_>, &mut Ctx<'_>) -> Result<Made, Refused>,
    ) -> Said {
        let Some(at) = self.by_session(session) else {
            return Err(nobody());
        };
        let made = {
            let (agents, pages) = (&self.agents, &self.pages);
            let ids = &mut self.ids;
            let others = |id: &str| {
                let has = |(index, agent): (usize, &Agent)| {
                    index != at && agent.artifacts.get(id).is_some()
                };
                agents.iter().enumerate().any(has)
            };
            let mut ctx = Ctx {
                now_ms: now,
                token_hex: &mut |bytes| Self::token(ids, bytes),
                taken: &|id| agents.iter().any(|agent| agent.artifacts.any(id).is_some()),
                published: &agents[at].media,
                elsewhere: &others,
                page: &|page| pages.get(&page.blob()).cloned(),
            };
            make(&agents[at].artifacts, agents[at].maker(), &mut ctx)?
        };
        Ok(self.kept(at, &made, now))
    }

    /// A change of one artifact, by the agent that holds it: kept, and shown with whether
    /// it changed anything.
    fn change(
        &mut self,
        at: Option<usize>,
        missing: &str,
        now: i64,
        change: impl FnOnce(&ArtifactBook) -> Result<(Artifact, bool), Refused>,
    ) -> Said {
        let Some(at) = at else {
            return refused(Why::Value, missing);
        };
        let (artifact, changed) = change(&self.agents[at].artifacts)?;
        if changed {
            self.agents[at].keep(&artifact, now);
        }
        Ok(json!([self.shown(at, &artifact, now), changed]))
    }

    /// A change to a plan, and the artifact the plan is shown as following it.
    fn plan(
        &mut self,
        at: Option<usize>,
        missing: &str,
        made_now: bool,
        now: i64,
        change: impl FnOnce(
            &GoalBook,
            Owner<'_>,
            &mut World<'_>,
        ) -> Result<(Change, String), clarp_kernel::goals::Refused>,
    ) -> Said {
        let Some(at) = at else {
            return refused(Why::Value, missing);
        };
        let (changed, title) = {
            let agent = &self.agents[at];
            let ids = &mut self.ids;
            let mut world = World {
                now_ms: now,
                native_session: "",
                queue_revision: 0,
                token_hex: &mut |bytes| Self::token(ids, bytes),
                has_job: &|_, _| false,
            };
            let owner = Owner {
                agent_id: &agent.agent_id,
                session: &agent.session,
            };
            change(&agent.goals, owner, &mut world).map_err(|why| Refused {
                why: Why::Value,
                message: why.0,
            })?
        };
        let record = self.agents[at].goals.record(&changed, now);
        self.agents[at].goals.apply(&record);
        let synced = {
            let agents = &self.agents;
            let ids = &mut self.ids;
            let mut ctx = Ctx {
                now_ms: now,
                token_hex: &mut |bytes| Self::token(ids, bytes),
                taken: &|id| agents.iter().any(|agent| agent.artifacts.any(id).is_some()),
                published: &agents[at].media,
                elsewhere: &|_| false,
                page: &|_| None,
            };
            agents[at].artifacts.plan_synced(
                agents[at].maker(),
                &changed.plan,
                &title,
                made_now,
                &mut ctx,
            )?
        };
        self.agents[at].keep(&synced, now);
        Ok(self.agents[at]
            .goals
            .get(&changed.plan.plan_id, now)
            .unwrap())
    }

    /// The requests whose time ran out are closed before anything is answered about them.
    fn expire(&mut self, now: i64) {
        for agent in &mut self.agents {
            for artifact in agent.artifacts.expired(now) {
                agent.keep(&artifact, now);
            }
        }
    }

    /// An agent publishes a file, the way the Host does it: read by the kernel's rules, in
    /// the order the Python Host refuses in, with an id counted as the vectors' Host counts.
    fn publish(&mut self, args: &Value, now: i64) -> Said {
        let hex = text(args, "blob_hex");
        let blob: Vec<u8> = (0..hex.len())
            .step_by(2)
            .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).unwrap())
            .collect();
        let session = text(args, "session");
        if session.trim().is_empty() {
            return refused(Why::Value, "session required");
        }
        if blob.is_empty() || blob.len() > media::MAX_BYTES {
            return refused(Why::Value, "bad size");
        }
        let Some(at) = self.by_session(&session) else {
            return refused(Why::Value, "unknown session");
        };
        let read = media::read(
            &blob,
            &text(args, "source_name"),
            &text(args, "content_type"),
        );
        let reading = match read {
            Ok(reading) => reading,
            Err(why) => return refused(Why::Value, why.words()),
        };
        self.files += 1;
        let sha256: String = Sha256::digest(&blob)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let asset = Asset {
            asset_id: format!("asset_{:024}", self.files),
            session: self.agents[at].session.clone(),
            source_name: reading.name,
            sha256,
            extension: reading.extension,
            mime_type: reading.mime,
            bytes: blob.len() as u64,
            width: reading.width,
            height: reading.height,
            caption: media::kept_caption(&text(args, "caption")),
            created_by: media::kept_publisher(""),
            created_at: now,
            named_only: false,
        };
        // A record is read as it was written.
        let read: Asset = serde_json::from_slice(&serde_json::to_vec(&asset).unwrap()).unwrap();
        assert_eq!(asset, read);
        self.agents[at].media.apply(&read);
        let mut row = asset.public();
        row["sha256"] = json!(asset.sha256);
        Ok(row)
    }

    fn call(&mut self, name: &str, args: &Value, now: i64) -> Said {
        let given = |name: &str| args.get(name).filter(|value| !value.is_null());
        match name {
            "publish" => self.publish(args, now),
            "create" => {
                let new = New {
                    kind: text(args, "type"),
                    title: text(args, "title"),
                    summary: text(args, "summary"),
                    status: given("status")
                        .map_or("ready".to_string(), |s| s.as_str().unwrap().to_string()),
                    reference_id: text(args, "reference_id"),
                    payload: args.get("payload").cloned(),
                    artifact_id: text(args, "artifact_id"),
                    expected_version: args.get("expected_version").cloned(),
                    validate_only: false,
                    source_trace_id: String::new(),
                };
                let nobody = || artifacts::create_of_nobody(&new);
                self.make(&text(args, "session"), now, nobody, |book, maker, ctx| {
                    book.create(maker, &new, ctx)
                })
            }
            "create_decision" => {
                let mut ask = Ask {
                    title: text(args, "title"),
                    payload: args.get("payload").cloned(),
                    reference_id: text(args, "reference_id"),
                    ..Ask::default()
                };
                let set = |slot: &mut Value, name: &str| {
                    if let Some(value) = args.get(name) {
                        *slot = value.clone();
                    }
                };
                set(&mut ask.question, "question");
                set(&mut ask.context, "context");
                set(&mut ask.yes_label, "yes_label");
                set(&mut ask.no_label, "no_label");
                set(&mut ask.response_type, "response_type");
                set(&mut ask.blocks_progress, "blocks_progress");
                set(&mut ask.priority_reason, "priority_reason");
                set(&mut ask.urgency, "urgency");
                set(&mut ask.response_effort, "response_effort");
                ask.expires_at = args.get("expires_at").cloned();
                ask.options = args.get("options").cloned();
                ask.allow_custom_text = args.get("allow_custom_text").cloned();
                ask.recommended_option_id = args.get("recommended_option_id").cloned();
                ask.deadline_at = args.get("deadline_at").cloned();
                ask.input_hint = args.get("input_hint").cloned();
                let nobody = || decisions::ask_of_nobody(&ask, now);
                self.make(&text(args, "session"), now, nobody, |book, maker, ctx| {
                    let artifact = book.ask(maker, &ask, ctx)?;
                    Ok(Made {
                        artifact,
                        page: None,
                        changed: true,
                    })
                })
            }
            "get" => {
                let found = artifacts::get(&self.shelves(), &text(args, "artifact_id"), now);
                Ok(self.answered(found.unwrap_or(Value::Null)))
            }
            // What a report was, version by version, or one version with its page.
            "revisions" => {
                let id = text(args, "artifact_id");
                let Some(at) = self.holding(&id) else {
                    return refused(Why::Value, "read-only report not found");
                };
                let number = |name: &str, otherwise: i64| {
                    given(name).and_then(Value::as_i64).unwrap_or(otherwise)
                };
                let version = given("version").and_then(Value::as_str);
                let (limit, offset) = (number("limit", 50), number("offset", 0));
                let book = &self.agents[at].artifacts;
                match book.revisions(&id, version, limit, offset)? {
                    Revisions::Listed(listed) => Ok(json!(listed)),
                    Revisions::One(mut one) => {
                        let page = one.get_mut("payload").and_then(reports::named_page);
                        let page = page.expect("a revision names its page");
                        one["payload"]["content"] = json!(self.pages[&page.blob()]);
                        Ok(one)
                    }
                }
            }
            // An artifact as a list shows it: without the payload its fields were taken
            // from, and in a summary without a report's page, which is named instead.
            "represent" => {
                let found = artifacts::get(&self.shelves(), &text(args, "artifact_id"), now);
                let mut row = found.unwrap_or(Value::Null);
                let page = reports::named_page(&mut row);
                if let Some(fields) = row.as_object_mut() {
                    fields.remove("payload");
                }
                match page {
                    Some(page) if args["representation"] == "summary-v1" => {
                        reports::without_page(&mut row, &page);
                    }
                    Some(page) => reports::with_page(&mut row, &self.pages[&page.blob()]),
                    None => {}
                }
                Ok(row)
            }
            "list" => {
                self.expire(now);
                let listing = Listing {
                    session: text(args, "session"),
                    agent_id: text(args, "agent_id"),
                    kind: text(args, "type"),
                    search: text(args, "search"),
                    created_from: given("created_from").and_then(Value::as_i64),
                    created_to: given("created_to").and_then(Value::as_i64),
                    limit: given("limit").and_then(Value::as_i64).unwrap_or(100),
                    offset: given("offset").and_then(Value::as_i64).unwrap_or(0),
                    by_creation: args["order"] == "created",
                };
                let rows = artifacts::list(&self.shelves(), &listing, now);
                let rows: Vec<Value> = rows.into_iter().map(|row| self.answered(row)).collect();
                Ok(json!(rows))
            }
            "list_pinned" => {
                let limit = given("limit").and_then(Value::as_i64).unwrap_or(100);
                let rows = artifacts::list_pinned(&self.shelves(), limit, now);
                let rows: Vec<Value> = rows.into_iter().map(|row| self.answered(row)).collect();
                Ok(json!(rows))
            }
            "update" => {
                let id = text(args, "artifact_id");
                let Some(at) = self
                    .agents
                    .iter()
                    .position(|a| a.artifacts.get(&id).is_some())
                else {
                    return refused(Why::Value, "artifact not found");
                };
                let made = {
                    let (agents, pages) = (&self.agents, &self.pages);
                    let ids = &mut self.ids;
                    let mut ctx = Ctx {
                        now_ms: now,
                        token_hex: &mut |bytes| Self::token(ids, bytes),
                        taken: &|id| agents.iter().any(|agent| agent.artifacts.any(id).is_some()),
                        published: &agents[at].media,
                        elsewhere: &|_| false,
                        page: &|page| pages.get(&page.blob()).cloned(),
                    };
                    agents[at].artifacts.update(&id, &args["data"], &mut ctx)?
                };
                Ok(self.kept(at, &made, now))
            }
            "archive" => {
                let Some(archived) = args["archived"].as_bool() else {
                    return refused(Why::Value, "archived must be a boolean");
                };
                let expected =
                    decisions::integer(&args["expected_updated_at"], "expected_updated_at", 0)?;
                let id = text(args, "artifact_id");
                let at = self
                    .agents
                    .iter()
                    .position(|a| a.artifacts.get(&id).is_some());
                self.change(at, "artifact not found", now, |book| {
                    book.archive(&id, archived, expected, now)
                })
            }
            "discard" => {
                let expected =
                    decisions::integer(&args["expected_updated_at"], "expected_updated_at", 0)?;
                let id = text(args, "artifact_id");
                self.change(self.holding(&id), "artifact not found", now, |book| {
                    book.discard(&id, expected, now)
                })
            }
            "pin" => {
                let Some(pinned) = args["pinned"].as_bool() else {
                    return refused(Why::Value, "pinned must be a boolean");
                };
                let id = text(args, "artifact_id");
                let at = self
                    .agents
                    .iter()
                    .position(|a| a.artifacts.get(&id).is_some());
                self.change(at, "artifact not found", now, |book| {
                    book.pin(&id, pinned, now)
                })
            }
            "resolve" => {
                let none = Value::Null;
                let revision = args.get("expected_revision").unwrap_or(&none);
                let expected = decisions::integer(revision, "expected_revision", 1)?;
                self.expire(now);
                let id = text(args, "decision_id");
                let sessions: Vec<String> = self.agents.iter().map(|a| a.session.clone()).collect();
                // Who answers in the user's place, as the Python Host's `resolve` is told: the
                // sessions the owner names (what is no text among them passed over), and the
                // session of the turn that asks.
                let designated: Vec<String> = args["designated_sessions"]
                    .as_array()
                    .map(|listed| {
                        listed
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default();
                let by = OnBehalf {
                    session: args["on_behalf_session"].as_str(),
                    designated: &designated,
                    verified: args["verified_session"].as_str().unwrap_or_default(),
                };
                self.change(self.asked(&id), "decision not found", now, |book| {
                    let given = Given {
                        expected_revision: expected,
                        choice: args.get("choice"),
                        answer: args.get("answer"),
                    };
                    let known = |session: &str| sessions.iter().any(|known| known == session);
                    book.resolve(&id, &given, &by, &known, now)
                })
            }
            "dismiss" => {
                let expected =
                    decisions::integer(&args["expected_revision"], "expected_revision", 1)?;
                self.expire(now);
                let id = text(args, "decision_id");
                self.change(self.asked(&id), "decision not found", now, |book| {
                    book.dismiss(&id, expected, now)
                })
            }
            "withdraw" => {
                let session = text(args, "session");
                if session.trim().is_empty() {
                    return refused(Why::Value, "session required");
                }
                let id = text(args, "decision_id");
                self.change(self.asked(&id), "decision not found", now, |book| {
                    book.withdraw(&id, &session, now)
                })
            }
            "attention" => {
                self.expire(now);
                let flag = |name: &str| args[name].as_bool().unwrap_or(false);
                let items = decisions::attention(
                    &self.shelves(),
                    flag("include_questions"),
                    flag("include_archived"),
                    now,
                );
                Ok(json!(items))
            }
            // A page of Updates. A cursor is each Host's own: a scenario names the page it
            // goes on from, and what is compared of a cursor is whether there is one.
            "inbox" => {
                let representation = text(args, "representation");
                if !["", "flat-v1", "summary-v1"].contains(&representation.as_str()) {
                    return refused(Why::Value, "unsupported artifact representation");
                }
                let limit = given("limit").and_then(Value::as_i64).unwrap_or(100);
                let cursor = match given("after").and_then(Value::as_str) {
                    Some(name) => self.cursors.get(name).cloned().unwrap_or_default(),
                    None => text(args, "cursor"),
                };
                let page = updates::page(&self.shelves(), limit, &cursor, now)?;
                if let Some(name) = given("keep").and_then(Value::as_str) {
                    let next = page.next_cursor.clone().unwrap_or_default();
                    self.cursors.insert(name.to_string(), next);
                }
                let rows: Vec<Value> = page
                    .artifacts
                    .into_iter()
                    .map(|mut row| {
                        let named = reports::named_page(&mut row);
                        if representation.is_empty() {
                            return match named {
                                Some(named) => {
                                    reports::with_page(&mut row, &self.pages[&named.blob()]);
                                    row
                                }
                                None => row,
                            };
                        }
                        if let Some(fields) = row.as_object_mut() {
                            fields.remove("payload");
                        }
                        match named {
                            Some(named) if representation == "summary-v1" => {
                                reports::without_page(&mut row, &named);
                            }
                            Some(named) => {
                                reports::with_page(&mut row, &self.pages[&named.blob()]);
                            }
                            None => {}
                        }
                        row
                    })
                    .collect();
                Ok(json!({
                    "artifacts": rows, "next_cursor": page.next_cursor.is_some(),
                    "snapshot_revision": "<revision>", "total": page.total,
                    "index_version": updates::INDEX_VERSION,
                }))
            }
            // A user's answers to a form: an id that was used is looked for on the whole
            // Host before the form is.
            "submit" => {
                let id = text(args, "artifact_id");
                let sent = forms::sent(&id, args["data"].as_object().unwrap())?;
                let before = self
                    .agents
                    .iter()
                    .find_map(|agent| agent.artifacts.submission(&sent.submission_id));
                if let Some((form, previous)) = before {
                    return previous.again(&form.artifact_id, &sent);
                }
                let Some(at) = self.holding(&id) else {
                    return refused(Why::Value, "HTML form not found");
                };
                let (form, receipt) = self.agents[at].artifacts.submitted(&id, &sent, now)?;
                self.agents[at].keep(&form, now);
                Ok(receipt)
            }
            "pending_submissions" => {
                let mut rows: Vec<(i64, Value)> = Vec::new();
                for agent in &self.agents {
                    for (artifact_id, submission_id) in agent.artifacts.untold() {
                        let (_, kept) = agent.artifacts.submission(&submission_id).unwrap();
                        let row = json!({
                            "submission_id": submission_id, "artifact_id": artifact_id,
                            "version": kept.version, "session": agent.session,
                            "prompt": kept.prompt, "created_at": kept.created_at,
                            "synthesize_audio": kept.synthesize_audio,
                        });
                        rows.push((kept.created_at, row));
                    }
                }
                rows.sort_by_key(|(at, _)| *at);
                let rows: Vec<Value> = rows.into_iter().map(|(_, row)| row).collect();
                Ok(json!(rows))
            }
            "mark_submission_delivered" => {
                let id = text(args, "submission_id");
                for agent in &mut self.agents {
                    let form = agent.artifacts.submission(&id);
                    let form = form.map(|(form, _)| form.artifact_id.clone());
                    if let Some(form) = form
                        && let Some((form, _, _)) = agent.artifacts.told(&form, &id, now)
                    {
                        agent.keep(&form, now);
                    }
                }
                Ok(Value::Null)
            }
            "pending_deliveries" => {
                let shelves = self.shelves();
                let rows: Vec<Value> = decisions::pending_deliveries(&shelves)
                    .into_iter()
                    .map(|artifact| {
                        let mut row = decisions::delivery_row(artifact).unwrap();
                        row["prompt"] = json!(decisions::delivery_prompt(artifact).unwrap());
                        row
                    })
                    .collect();
                Ok(json!(rows))
            }
            "mark_delivered" => {
                let id = text(args, "decision_id");
                if let Some(at) = self.asked(&id)
                    && let Some(artifact) = self.agents[at].artifacts.delivered(&id, now)
                {
                    self.agents[at].keep(&artifact, now);
                }
                Ok(Value::Null)
            }
            "delivery_pending" => {
                let id = text(args, "decision_id");
                let pending = self.asked(&id).is_some_and(|at| {
                    let artifact = self.agents[at].artifacts.of_decision(&id).unwrap();
                    artifact.decision.as_ref().unwrap().delivery_pending()
                });
                Ok(json!(pending))
            }
            "has_blocking_decision" | "has_pending_decision" => {
                let id = text(args, "agent_id");
                let agent = self.agents.iter().find(|agent| agent.agent_id == id.trim());
                Ok(json!(agent.is_some_and(|agent| {
                    if name == "has_blocking_decision" {
                        agent.artifacts.has_blocking_decision(now)
                    } else {
                        agent.artifacts.has_pending_decision(now)
                    }
                })))
            }
            "cancel_for_agent" => {
                let id = text(args, "agent_id");
                if let Some(agent) = self.agents.iter_mut().find(|agent| agent.agent_id == id) {
                    for artifact in agent.artifacts.cancelled_with_agent(now) {
                        agent.keep(&artifact, now);
                    }
                }
                Ok(Value::Null)
            }
            "plan_create" => {
                let title = text(args, "title");
                let at = self.by_session(&text(args, "session"));
                self.plan(
                    at,
                    "agent session not found",
                    true,
                    now,
                    |goals, owner, world| {
                        let made = goals.create(
                            owner,
                            &text(args, "plan_id"),
                            &title,
                            &args["items"],
                            args.get("goal"),
                            world,
                        )?;
                        Ok((made, title.trim().to_string()))
                    },
                )
            }
            "plan_step" | "plan_act" | "plan_finish" => {
                let plan_id = text(args, "plan_id");
                let revision = args["revision"].as_i64().unwrap();
                self.plan(
                    self.of_plan(&plan_id),
                    "task plan not found",
                    false,
                    now,
                    |goals, _, world| {
                        let changed = match name {
                            "plan_step" => {
                                let item = item_key(&plan_id, &text(args, "item"), world.token_hex);
                                let detail = args.get("detail").and_then(Value::as_str);
                                goals.step(
                                    &item,
                                    Some(revision),
                                    &text(args, "status"),
                                    detail,
                                    world.now_ms,
                                )?
                            }
                            "plan_act" => goals.act(
                                &plan_id,
                                revision,
                                &text(args, "action"),
                                &args["data"],
                                world,
                            )?,
                            _ => goals.finish(
                                &plan_id,
                                "completed",
                                "",
                                Some(revision),
                                world.now_ms,
                            )?,
                        };
                        let title = changed.plan.title.clone();
                        Ok((changed, title))
                    },
                )
            }
            other => panic!("the vectors make a call this test does not know: {other}"),
        }
    }
}

#[test]
fn artifacts_and_decisions_answer_as_the_python_hosts_do() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/artifact-vectors.json")).unwrap();
    let mut wrong: Vec<String> = Vec::new();
    let mut compared = 0;
    for scenario in vectors["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let agents = scenario["agents"]
            .as_array()
            .unwrap()
            .iter()
            .map(|agent| Agent {
                agent_id: text(agent, "agent_id"),
                session: text(agent, "session"),
                persona: text(agent, "persona"),
                artifacts: ArtifactBook::new(),
                goals: GoalBook::new(),
                media: MediaBook::new(),
            });
        let mut fleet = Fleet {
            agents: agents.collect(),
            ids: 0,
            files: 0,
            pages: HashMap::new(),
            cursors: HashMap::new(),
        };
        for (number, call) in scenario["calls"].as_array().unwrap().iter().enumerate() {
            let what = call["call"].as_str().unwrap();
            let now = call["at"].as_i64().unwrap();
            let ids_before = fleet.ids;
            let said = fleet.call(what, &call["args"], now);
            let context = format!("{name}, call {number} ({what} {})", call["args"]);
            compared += 1;
            let mut found = Vec::new();
            match (&said, call.get("result"), call.get("error")) {
                (Ok(ours), Some(theirs), _) => {
                    let mut theirs = theirs.clone();
                    // The Python Host answers a discard with the row as it read it before:
                    // it still says pinned when the pin is gone. Here the row is what it is.
                    if what == "discard" {
                        theirs[0]["pinned"] = json!(!theirs[0]["pinned_at"].is_null());
                    }
                    differences("result", ours, &theirs, &mut found);
                }
                (Err(ours), _, Some(theirs)) => {
                    let kind = match ours.why {
                        // What the store of published files refuses has a name of its own.
                        Why::Value if what == "publish" => "MediaError",
                        // And so has a report that is sent answers.
                        Why::Value if ours.message == forms::READ_ONLY => "ReadOnlyForm",
                        // And a cursor of a list that is no longer the list.
                        Why::Value if ours.message == updates::STALE => "StaleCursor",
                        Why::Value => "ValueError",
                        Why::Permission => "PermissionError",
                        Why::Integrity => "IntegrityError",
                        Why::Spec(code) => code,
                    };
                    if kind != theirs["kind"] || ours.message != theirs["message"].as_str().unwrap()
                    {
                        found.push(format!(
                            "refused here as {kind}: {}; there as {theirs}",
                            ours.message
                        ));
                    }
                }
                (Ok(ours), None, Some(theirs)) => {
                    let ours = ours.to_string();
                    let shown: String = ours.chars().take(300).collect();
                    found.push(format!("answered here: {shown}; refused there: {theirs}"));
                }
                (Err(ours), Some(theirs), _) => {
                    let theirs = theirs.to_string();
                    let shown: String = theirs.chars().take(300).collect();
                    found.push(format!(
                        "refused here: {}; answered there: {shown}",
                        ours.message
                    ));
                }
                _ => found.push("the vector has neither a result nor an error".to_string()),
            }
            let used = fleet.ids - ids_before;
            if Some(used) != call["ids"].as_u64() {
                found.push(format!("ids made: here {used}, there {}", call["ids"]));
            }
            if !found.is_empty() {
                found.truncate(8);
                wrong.push(format!("{context}:\n    {}", found.join("\n    ")));
            }
        }
    }
    let shown: Vec<&str> = wrong.iter().take(30).map(String::as_str).collect();
    assert!(
        wrong.is_empty(),
        "{} of {compared} calls differ:\n{}",
        wrong.len(),
        shown.join("\n")
    );
    assert!(compared >= 500, "only {compared} calls were compared");
}
