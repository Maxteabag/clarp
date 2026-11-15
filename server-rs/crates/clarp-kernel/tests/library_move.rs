//! An agent's library moves with it: what one book shows, another takes and shows the same.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;

use clarp_kernel::artifacts::decisions::{Ask, Given, OnBehalf};
use clarp_kernel::artifacts::reports::{self, Revisions};
use clarp_kernel::artifacts::{Artifact, ArtifactBook, Ctx, Made, Maker, New};
use clarp_kernel::goals::GoalBook;
use clarp_kernel::media::MediaBook;
use serde_json::{Value, json};

const VERA: Maker<'static> = Maker {
    agent_id: "a1",
    session: "vera",
    persona: "Vera",
};

/// One agent's artifacts with the pages kept beside them, driven the way the Host does.
#[derive(Default)]
struct Shelf {
    book: ArtifactBook,
    pages: HashMap<String, String>,
    ids: u64,
}

impl Shelf {
    fn keep(&mut self, made: &Made) {
        if let (Some(content), Some(page)) = (&made.page, &made.artifact.page) {
            self.pages.insert(page.blob(), content.clone());
        }
        if made.changed {
            self.put(&made.artifact);
        }
    }

    fn put(&mut self, artifact: &Artifact) {
        let record = ArtifactBook::record(artifact, artifact.updated_at);
        let read = serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
        assert_eq!(record, read, "a record is read as it was written");
        self.book.apply(&read);
    }

    /// Does one thing to the book, with the world a call is given.
    fn with<T>(&mut self, now: i64, call: impl FnOnce(&ArtifactBook, &mut Ctx<'_>) -> T) -> T {
        let (book, pages, ids) = (&self.book, &self.pages, &mut self.ids);
        let media = MediaBook::new();
        let mut ctx = Ctx {
            now_ms: now,
            token_hex: &mut |bytes| {
                *ids += 1;
                format!("{:0width$x}", *ids, width = bytes * 2)
            },
            taken: &|id| book.any(id).is_some(),
            published: &media,
            elsewhere: &|_| false,
            page: &|page| pages.get(&page.blob()).cloned(),
        };
        call(book, &mut ctx)
    }

    /// An artifact as the Host answers it: with its page put back.
    fn shown(&self, artifact_id: &str, now: i64) -> Value {
        let artifact = self.book.get(artifact_id).unwrap();
        let mut row = artifact.public(VERA.persona, &GoalBook::new(), now);
        if let Some(page) = reports::named_page(&mut row) {
            reports::with_page(&mut row, &self.pages[&page.blob()]);
        }
        row
    }

    /// What a report was: its versions as the Host answers each of them, the newest first.
    fn was(&self, artifact_id: &str) -> Vec<Value> {
        let Ok(Revisions::Listed(listed)) = self.book.revisions(artifact_id, None, 100, 0) else {
            return Vec::new();
        };
        listed
            .iter()
            .map(|entry| {
                let version = entry["version"].as_str().unwrap();
                let one = self.book.revisions(artifact_id, Some(version), 100, 0);
                let Ok(Revisions::One(mut one)) = one else {
                    panic!("a listed version is not there: {version}");
                };
                let page = one
                    .get_mut("payload")
                    .and_then(reports::named_page)
                    .unwrap();
                one["payload"]["content"] = json!(self.pages[&page.blob()]);
                one
            })
            .collect()
    }
}

fn new(kind: &str, title: &str, id: &str, payload: Value) -> New {
    New {
        kind: kind.to_string(),
        title: title.to_string(),
        status: "ready".to_string(),
        artifact_id: id.to_string(),
        payload: Some(payload),
        ..New::default()
    }
}

#[test]
fn what_an_agent_made_and_asked_arrives_as_it_was() {
    // On one Host: notes that were pinned and put away, a report that was revised, a
    // request that waits for its user and one that was answered and not yet told.
    let mut there = Shelf::default();
    let notes = new(
        "document",
        "Notes",
        "notes-1",
        json!({ "content": "# Notes" }),
    );
    let made = there
        .with(1_000, |book, ctx| book.create(VERA, &notes, ctx))
        .unwrap();
    there.keep(&made);
    let (pinned, _) = there.book.pin("notes-1", true, 1_500).unwrap();
    there.put(&pinned);
    let at = there.book.get("notes-1").unwrap().updated_at;
    let (away, _) = there.book.archive("notes-1", true, at, 1_600).unwrap();
    there.put(&away);

    let page = |content: &str, version: &str| json!({ "content": content, "version": version, "read_only": true });
    let report = new(
        "html_form",
        "A report",
        "report-1",
        page("<p>One.</p>", "v1"),
    );
    let made = there
        .with(2_000, |book, ctx| book.create(VERA, &report, ctx))
        .unwrap();
    there.keep(&made);
    let again = New {
        expected_version: Some(json!("v1")),
        summary: "Day two".to_string(),
        ..new(
            "html_form",
            "A report",
            "report-1",
            page("<p>Two.</p>", "v2"),
        )
    };
    let made = there
        .with(3_000, |book, ctx| book.create(VERA, &again, ctx))
        .unwrap();
    there.keep(&made);

    let ask = |title: &str| Ask {
        title: title.to_string(),
        question: json!("Ship it?"),
        context: json!("It is ready."),
        blocks_progress: json!(true),
        priority_reason: json!("The release waits for it"),
        ..Ask::default()
    };
    let open = there
        .with(4_000, |book, ctx| book.ask(VERA, &ask("Open"), ctx))
        .unwrap();
    there.put(&open);
    let closed = there
        .with(5_000, |book, ctx| book.ask(VERA, &ask("Answered"), ctx))
        .unwrap();
    there.put(&closed);
    let decision = closed.decision.as_ref().unwrap();
    let yes = json!("accepted");
    let given = Given {
        expected_revision: decision.revision,
        choice: Some(&yes),
        answer: None,
    };
    let (answered, _) = there
        .book
        .resolve(
            &decision.decision_id,
            &given,
            &OnBehalf::default(),
            &|_| false,
            6_000,
        )
        .unwrap();
    there.put(&answered);
    assert!(answered.decision.as_ref().unwrap().delivery_pending());

    // The agent moves. Each artifact is taken as it is shown, a report with what it was.
    let ids: Vec<String> = there
        .book
        .artifacts()
        .map(|a| a.artifact_id.clone())
        .collect();
    assert_eq!(ids.len(), 4);
    let mut here = Shelf::default();
    for id in &ids {
        let (shown, was) = (there.shown(id, 9_000), there.was(id));
        let arrived = here
            .book
            .taken_over(VERA, &shown, &was, &|_| false)
            .unwrap();
        for content in &arrived.pages {
            here.pages
                .insert(reports::Page::of(content).blob(), content.clone());
        }
        here.put(&arrived.artifact);
    }
    for id in &ids {
        assert_eq!(here.shown(id, 9_000), there.shown(id, 9_000), "{id}");
        assert_eq!(here.was(id), there.was(id), "{id}");
    }
    assert_eq!(here.pages.len(), 2, "the report's two pages, each once");
    // The answer that waited to be told is still to be told here, once.
    assert_eq!(here.book.undelivered(), there.book.undelivered());
    assert_eq!(here.book.undelivered().len(), 1);
    // And the report goes on here: its next version follows the one it has.
    let next = New {
        expected_version: Some(json!("v2")),
        ..new(
            "html_form",
            "A report",
            "report-1",
            page("<p>Three.</p>", "v3"),
        )
    };
    let made = here
        .with(10_000, |book, ctx| book.create(VERA, &next, ctx))
        .unwrap();
    here.keep(&made);
    assert_eq!(here.was("report-1").len(), 3);

    // What is another agent's, or here already, does not arrive.
    let shown = there.shown("notes-1", 9_000);
    let other = Maker {
        agent_id: "b2",
        session: "omar",
        persona: "Omar",
    };
    let refused = ArtifactBook::new().taken_over(other, &shown, &[], &|_| false);
    assert_eq!(
        refused.unwrap_err().message,
        "the artifact is not this agent's"
    );
    let refused = here
        .book
        .taken_over(VERA, &shown, &[], &|id| here.book.any(id).is_some());
    assert_eq!(
        refused.unwrap_err().message,
        "an artifact with this id is here already"
    );
}
