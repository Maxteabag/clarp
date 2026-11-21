//! Advisory gallery events for committed, terminal portrait-generation runs.

use std::sync::Arc;

use clarp_kernel::portrait_generation::MediaWindow;
use clarp_ports::clock::Clock;
use serde_json::json;

use crate::{gallery::Writer, hub::Hub, unkept::Log};

#[derive(Clone)]
pub struct Announcer {
    pub writer: Writer,
    pub hub: Arc<Hub>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub log: Log,
}

impl Announcer {
    pub(crate) async fn tell(&self, window: MediaWindow) {
        let hub = self.hub.clone();
        let clock = self.clock.clone();
        let said = self
            .writer
            .ask(move |store| {
                for hint in store.portrait_hints(&window)? {
                    hub.publish(
                        json!({
                            "type": "media-changed",
                            "session": hint.session,
                            "agent_id": hint.agent_id,
                            "asset_id": hint.asset_id,
                            "change": hint.change,
                            "at": hint.at,
                        }),
                        clock.now_ms(),
                    );
                }
                Ok(())
            })
            .await;
        if let Err(why) = said {
            self.failed(why.words());
        }
    }

    pub(crate) fn failed(&self, why: &str) {
        self.log.line(&format!(
            "clarp-host: portrait gallery change could not be announced: {why}"
        ));
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_kernel::gallery::{Deletion, Image};
    use clarp_kernel::media::Asset;
    use clarp_kernel::portrait_generation::CREATED_BY_PREFIX;
    use clarp_ports::{blobs::BlobEngine, clock::SystemClock, kv::KeyValueEngine};
    use clarp_store_mem::{MemBlobs, MemKeyValue};
    use serde_json::Value;

    fn image(id: &str, session: &str, at: i64, deleted: Option<i64>, portrait: bool) -> Image {
        let mut row = Image::new(
            Asset {
                asset_id: id.to_owned(),
                session: session.to_owned(),
                source_name: "portrait.png".into(),
                sha256: format!("digest-{id}"),
                mime_type: "image/png".into(),
                extension: ".png".into(),
                bytes: 1,
                width: None,
                height: None,
                caption: String::new(),
                created_by: if portrait {
                    format!("{CREATED_BY_PREFIX}local")
                } else {
                    "agent".into()
                },
                created_at: at,
                named_only: false,
            },
            format!("{session}-id"),
        );
        row.deletion = deleted.map(|at| Deletion {
            at,
            by: String::new(),
        });
        row
    }

    #[tokio::test]
    async fn terminal_hints_use_event_time_and_do_not_repeat_after_reopen() {
        let path = std::path::Path::new("portrait-hints");
        let db = MemKeyValue::new();
        let blobs = MemBlobs::new().open(path).unwrap();
        let open = || {
            let store = crate::gallery::Store::open(db.open(path).unwrap(), blobs.clone()).unwrap();
            Writer::start(store).unwrap()
        };
        let writer = open();
        writer
            .ask(|store| {
                store.import(vec![
                    image("late-kept", "oak", 15, None, true),
                    image("discarded", "oak", 11, Some(12), true),
                    image("early-kept", "oak", 12, None, true),
                    image("another-chat", "elm", 12, None, true),
                    image("another-producer", "oak", 12, None, false),
                    image("older", "oak", 9, Some(14), true),
                    image("after-end", "oak", 21, None, true),
                    image("already-told", "oak", 12, None, true),
                ])?;
                store.remember_hint("already-told", "created")?;
                Ok(())
            })
            .await
            .unwrap();
        let clock: Arc<dyn Clock + Send + Sync> = Arc::new(SystemClock);
        let hub = Arc::new(Hub::new(clock.now_ms()));
        let announcer = Announcer {
            writer,
            hub: hub.clone(),
            clock: clock.clone(),
            log: Log::stderr(clock),
        };
        announcer
            .tell(MediaWindow {
                session: String::new(),
                since: 10,
                until: 20,
            })
            .await;
        announcer
            .tell(MediaWindow {
                session: "oak".into(),
                since: 22,
                until: 25,
            })
            .await;
        assert!(hub.subscribe(None).0.is_empty());
        let window = MediaWindow {
            session: "oak".into(),
            since: 10,
            until: 20,
        };
        announcer.tell(window.clone()).await;
        let events = || {
            hub.subscribe(None)
                .0
                .iter()
                .map(|row| {
                    let event: Value = serde_json::from_str(&row.json).unwrap();
                    assert_eq!(event["session"], "oak");
                    assert_eq!(event["agent_id"], "oak-id");
                    assert_eq!(event["type"], "media-changed");
                    assert!(event.get("favorite").is_none());
                    (
                        event["asset_id"].as_str().unwrap().to_owned(),
                        event["change"].as_str().unwrap().to_owned(),
                        event["at"].as_i64().unwrap(),
                    )
                })
                .collect::<Vec<_>>()
        };
        let actual = events();
        assert_eq!(
            actual,
            vec![
                ("discarded".into(), "deleted".into(), 12),
                ("early-kept".into(), "created".into(), 12),
                ("late-kept".into(), "created".into(), 15),
            ]
        );
        announcer.tell(window.clone()).await;
        let restarted = Announcer {
            writer: open(),
            ..announcer
        };
        restarted.tell(window).await;
        assert_eq!(events(), actual);
    }

    #[tokio::test]
    async fn an_announcement_refusal_is_logged_without_changing_the_image() {
        use clarp_ports::kv::{KeyValue, StoreError, Write};
        struct RejectHints(Box<dyn KeyValue>);
        impl KeyValue for RejectHints {
            fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
                self.0.get(table, key)
            }
            fn put(&mut self, table: &str, key: &str, body: &[u8]) -> Result<(), StoreError> {
                self.0.put(table, key, body)
            }
            fn put_together(&mut self, writes: &[Write]) -> Result<(), StoreError> {
                if writes.iter().any(|(table, _, _)| table == "media-hints") {
                    return Err(StoreError("fixture refusal of advisory hint".into()));
                }
                self.0.put_together(writes)
            }
            fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
                self.0.delete(table, key)
            }
            fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
                self.0.list(table)
            }
        }
        let path = std::path::Path::new("failed-portrait-hint");
        let db = RejectHints(MemKeyValue::new().open(path).unwrap());
        let blobs = MemBlobs::new().open(path).unwrap();
        let mut store = crate::gallery::Store::open(Box::new(db), blobs).unwrap();
        let kept = image("kept", "oak", 11, None, true);
        store.publish(kept.clone(), b"i").unwrap();
        let writer = Writer::start(store).unwrap();
        let clock: Arc<dyn Clock + Send + Sync> = Arc::new(SystemClock);
        let hub = Arc::new(Hub::new(clock.now_ms()));
        let lines = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let told = lines.clone();
        let announcer = Announcer {
            writer: writer.clone(),
            hub: hub.clone(),
            clock: clock.clone(),
            log: Log::to(
                move |line| told.lock().unwrap().push(line.to_owned()),
                clock,
            ),
        };
        announcer
            .tell(MediaWindow {
                session: "oak".into(),
                since: 10,
                until: 20,
            })
            .await;
        assert!(hub.subscribe(None).0.is_empty());
        let lines = lines.lock().unwrap().clone();
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert!(lines[0].contains("fixture refusal of advisory hint"));
        assert_eq!(
            writer.ask(|store| store.content("kept")).await.unwrap(),
            (kept, 1)
        );
    }
}
