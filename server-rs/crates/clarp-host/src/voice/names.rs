//! Voice labels are fetched and persisted off the profile and catalogue read paths.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::SystemTime;

use clarp_kernel::said::{self, CARTESIA, ELEVENLABS};
use clarp_kernel::voice_names::{self, CatalogueVoice, Snapshot};
use clarp_speech::LookupError;
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::Mouth;
use crate::ids;

type ProviderNames = BTreeMap<String, String>;
type Used = BTreeMap<String, BTreeMap<String, BTreeSet<String>>>;

fn record(used: &mut Used, provider: &str, id: String, holder: &str) {
    if id.is_empty() {
        return;
    }
    let holders = used
        .entry(provider.into())
        .or_default()
        .entry(id)
        .or_default();
    if !holder.is_empty() {
        holders.insert(holder.into());
    }
}

#[derive(Default)]
struct Stored {
    stamp: Option<(SystemTime, u64)>,
    snapshot: Snapshot,
    hash: Option<String>,
}

pub(super) struct Desk {
    path: PathBuf,
    current: Mutex<Stored>,
    refreshing: Mutex<()>,
}

struct Provider {
    key: String,
    endpoint: String,
}

#[derive(Default)]
struct Sources {
    cartesia: Option<Provider>,
    elevenlabs: Option<Provider>,
}

impl Desk {
    pub(super) fn new(path: PathBuf) -> Self {
        Self {
            path,
            current: Mutex::default(),
            refreshing: Mutex::default(),
        }
    }

    pub(super) fn snapshot(&self) -> (Snapshot, Option<String>) {
        let current = self.current.lock().unwrap_or_else(PoisonError::into_inner);
        (current.snapshot.clone(), current.hash.clone())
    }

    fn publish(&self, stored: Stored) {
        *self.current.lock().unwrap_or_else(PoisonError::into_inner) = stored;
    }

    fn read(&self) -> io::Result<()> {
        let meta = match fs::metadata(&self.path) {
            Ok(meta) => meta,
            Err(why) if why.kind() == io::ErrorKind::NotFound => {
                self.publish(Stored::default());
                return Ok(());
            }
            Err(why) => return Err(why),
        };
        let stamp = Some((meta.modified()?, meta.len()));
        if self
            .current
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .stamp
            == stamp
        {
            return Ok(());
        }
        let raw = fs::read(&self.path)?;
        let hash = Some(digest(&raw));
        let snapshot = match serde_json::from_slice::<Value>(&raw) {
            Ok(value) => Snapshot::from_file(&value),
            Err(why) => {
                eprintln!("voiceNamesReadFail {why}");
                Snapshot::default()
            }
        };
        self.publish(Stored {
            stamp,
            snapshot,
            hash,
        });
        Ok(())
    }

    fn start_due(&self) -> bool {
        let stamp = self
            .current
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .stamp;
        let age = stamp.map(|(modified, _)| modified.elapsed().unwrap_or_default());
        voice_names::start_due(age)
    }

    fn refresh(&self, sources: &Sources, used: &Used) -> io::Result<()> {
        let Ok(_refresh) = self.refreshing.try_lock() else {
            return Ok(());
        };
        let fetched = sources.fetch(used);
        if fetched.names.is_empty() {
            return Ok(());
        }
        self.read()?;
        let (before, old_hash) = self.snapshot();
        let mut snapshot = before.clone();
        let newly_gone = snapshot.apply(fetched);
        let hash = if old_hash.is_some() && snapshot == before {
            File::open(&self.path)?.set_modified(SystemTime::now())?;
            old_hash
        } else {
            let raw = serde_json::to_vec(&snapshot).map_err(io::Error::other)?;
            self.replace(&raw)?;
            Some(digest(&raw))
        };
        let meta = fs::metadata(&self.path)?;
        self.publish(Stored {
            stamp: Some((meta.modified()?, meta.len())),
            snapshot,
            hash,
        });
        for (provider, id) in newly_gone {
            let holders = used
                .get(&provider)
                .and_then(|voices| voices.get(&id))
                .into_iter()
                .flatten()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(",");
            eprintln!("voiceGone provider={provider} id={id} agents={holders}");
        }
        Ok(())
    }

    fn replace(&self, raw: &[u8]) -> io::Result<()> {
        let tmp = self.path.with_extension(format!("{}.tmp", ids::hex(8)));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&tmp)?;
        let written = file
            .write_all(raw)
            .and_then(|()| file.sync_all())
            .and_then(|()| fs::rename(&tmp, &self.path));
        if written.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        written
    }
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
        .chars()
        .take(16)
        .collect()
}

impl Sources {
    fn fetch(&self, used: &Used) -> Snapshot {
        let mut fetched = Snapshot::default();
        let ids = |provider: &str| {
            used.get(provider)
                .into_iter()
                .flat_map(|voices| voices.keys().cloned())
        };
        if let Some(source) = &self.cartesia {
            match clarp_speech::cartesia::all_voices(&source.key, &source.endpoint) {
                Ok(rows) => {
                    let today: BTreeMap<_, _> = rows
                        .iter()
                        .filter_map(|row| {
                            let id = row["id"].as_str().filter(|id| !id.is_empty())?;
                            Some((
                                id.to_owned(),
                                CatalogueVoice {
                                    language: row["language"]
                                        .as_str()
                                        .unwrap_or_default()
                                        .to_owned(),
                                    public: clarp_kernel::python::truthy(&row["is_public"]),
                                    owner: clarp_kernel::python::truthy(&row["is_owner"]),
                                },
                            ))
                        })
                        .collect();
                    let mut listed: ProviderNames = rows
                        .iter()
                        .filter_map(|row| {
                            let id = row["id"].as_str().filter(|id| !id.is_empty())?;
                            let name = row["name"].as_str()?.trim();
                            (!name.is_empty()).then(|| (id.to_owned(), name.to_owned()))
                        })
                        .collect();
                    let missing = ids(CARTESIA).filter(|id| !today.contains_key(id)).collect();
                    let gone = lookup(&missing, &mut listed, CARTESIA, |id| {
                        clarp_speech::cartesia::voice_by_id(&source.key, &source.endpoint, id)
                            .map(|row| row["name"].as_str().unwrap_or("").trim().to_owned())
                    });
                    fetched.names.insert(CARTESIA.into(), listed);
                    fetched.catalogue.insert(CARTESIA.into(), today);
                    fetched.gone.insert(CARTESIA.into(), gone);
                }
                Err(why) => eprintln!("voiceNamesFetchFail {CARTESIA}: {why}"),
            }
        }
        let eleven_ids: BTreeSet<_> = ids(ELEVENLABS)
            .filter(|id| clarp_kernel::voice_profile::elevenlabs_id_usable(Some(id)))
            .collect();
        if let Some(source) = &self.elevenlabs
            && !eleven_ids.is_empty()
        {
            let mut found = ProviderNames::new();
            let gone = lookup(&eleven_ids, &mut found, ELEVENLABS, |id| {
                clarp_speech::eleven_voice::voice_by_id(&source.key, &source.endpoint, id)
            });
            fetched.names.insert(ELEVENLABS.into(), found);
            fetched.gone.insert(ELEVENLABS.into(), gone);
        }
        fetched
    }
}

fn lookup(
    used: &BTreeSet<String>,
    found: &mut ProviderNames,
    provider: &str,
    mut fetch_one: impl FnMut(&str) -> Result<String, LookupError>,
) -> BTreeSet<String> {
    let mut gone = BTreeSet::new();
    for id in voice_names::lookups(used, found) {
        match fetch_one(id) {
            Ok(name) if !name.is_empty() => {
                found.insert(id.to_owned(), name);
            }
            Err(LookupError::Http(404)) => {
                gone.insert(id.to_owned());
            }
            Err(why) => eprintln!("voiceNamesLookupFail {provider} {id}: {why}"),
            Ok(_) => {}
        }
    }
    gone
}

impl Mouth {
    /// Read the Host's published file once, then keep it current off the request path.
    pub async fn start_voice_names(self: &Arc<Self>) {
        let mouth = self.clone();
        match tokio::task::spawn_blocking(move || mouth.voice_names.read()).await {
            Ok(Ok(())) => {}
            Ok(Err(why)) => eprintln!("voiceNamesReadFail {why}"),
            Err(why) => eprintln!("voiceNamesWorkerFail {why}"),
        }
        if std::env::var("CLARP_VOICE_NAMES_WORKER").as_deref() == Ok("0") {
            return;
        }
        let mouth = Arc::downgrade(self);
        tokio::spawn(async move {
            tokio::time::sleep(voice_names::FIRST_AFTER).await;
            let mut first = true;
            while let Some(mouth) = mouth.upgrade() {
                let result = tokio::task::spawn_blocking(move || {
                    if first {
                        mouth.voice_names.read()?;
                        if !mouth.voice_names.start_due() {
                            return Ok(());
                        }
                    }
                    mouth.refresh_voice_names()
                })
                .await;
                match result {
                    Ok(Ok(())) => {}
                    Ok(Err(why)) => eprintln!("voiceNamesWorkerFail {why}"),
                    Err(why) => eprintln!("voiceNamesWorkerFail {why}"),
                }
                first = false;
                tokio::time::sleep(voice_names::REFRESH_EVERY).await;
            }
        });
    }

    fn refresh_voice_names(&self) -> io::Result<()> {
        let directory = self.directory();
        let owners = self.owners_from(&directory);
        let sources = Sources {
            cartesia: super::OwnerFile::key_from(Some(&owners), CARTESIA).map(|key| Provider {
                key,
                endpoint: self.cartesia_list_at(),
            }),
            elevenlabs: super::OwnerFile::key_from(Some(&owners), ELEVENLABS).map(|key| Provider {
                key,
                endpoint: self.eleven_at.clone(),
            }),
        };
        if sources.cartesia.is_none() && sources.elevenlabs.is_none() {
            return Ok(());
        }

        let mut used = Used::new();
        for (holder, id) in crate::owner::texts_at(&owners, &["cartesia", "voices"]) {
            record(&mut used, CARTESIA, id, &holder);
        }
        let pool = owners
            .get("cartesia")
            .and_then(toml::Value::as_table)
            .and_then(|cartesia| cartesia.get("pool"))
            .and_then(toml::Value::as_array);
        for id in pool.into_iter().flatten().filter_map(toml::Value::as_str) {
            record(&mut used, CARTESIA, id.to_owned(), "");
        }
        let overrides = owners
            .get("tts")
            .and_then(|tts| tts.get("agents"))
            .and_then(toml::Value::as_table);
        for (holder, table) in overrides.into_iter().flatten() {
            for (provider, key) in [
                (CARTESIA, "cartesia_voice"),
                (ELEVENLABS, "elevenlabs_voice"),
            ] {
                if let Some(id) = table.get(key).and_then(toml::Value::as_str) {
                    record(&mut used, provider, id.trim().to_owned(), holder);
                }
            }
        }
        let mut collect = |raw: &str, holder: &str| {
            for (provider, id) in said::voice_map(Some(raw)) {
                if matches!(provider.as_str(), CARTESIA | ELEVENLABS) && !id.is_empty() {
                    record(&mut used, &provider, id, holder);
                }
            }
        };
        for member in directory.members.iter().chain(&directory.janitors) {
            let holder = if member.record.persona.is_empty() {
                &member.record.session
            } else {
                &member.record.persona
            };
            collect(&member.record.voice_id, holder);
        }
        let [roster, cartesia, gemini] = crate::owner::personas(&owners);
        for persona in directory.personas.listed(&clarp_kernel::personas::Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        }) {
            if let Some(raw) = persona["voice_id"].as_str() {
                let holder = persona["name"].as_str().unwrap_or_default();
                collect(raw, holder);
            }
        }
        self.voice_names.refresh(&sources, &used)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::{BufRead, BufReader};
    use std::net::{TcpListener, TcpStream};
    use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
    use std::thread;

    struct Catalogue {
        addr: std::net::SocketAddr,
        failures: Arc<AtomicU8>,
        asked: Arc<Mutex<Vec<String>>>,
        stop: Arc<AtomicBool>,
        worker: Option<thread::JoinHandle<()>>,
    }

    impl Catalogue {
        fn start() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let mut server = Self {
                addr: listener.local_addr().unwrap(),
                failures: Arc::default(),
                asked: Arc::default(),
                stop: Arc::default(),
                worker: None,
            };
            let (asked, failures, stop) = (
                server.asked.clone(),
                server.failures.clone(),
                server.stop.clone(),
            );
            server.worker = Some(thread::spawn(move || {
                for stream in listener.incoming() {
                    let Ok(mut stream) = stream else { break };
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    stream
                        .set_read_timeout(Some(std::time::Duration::from_secs(2)))
                        .unwrap();
                    let mut reader = BufReader::new(&stream);
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    let path = line
                        .split_whitespace()
                        .nth(1)
                        .unwrap_or_default()
                        .to_owned();
                    loop {
                        line.clear();
                        let read = reader.read_line(&mut line).unwrap();
                        if read == 0 || line == "\r\n" {
                            break;
                        }
                    }
                    asked.lock().unwrap().push(path.clone());
                    let failures = failures.load(Ordering::SeqCst);
                    let elevenlabs = path.starts_with("/v1/voices/");
                    let (status, body) = if failures == 1 || (failures == 2 && !elevenlabs) {
                        (503, json!({}))
                    } else if path.contains("starting_after=next%2Fpage") {
                        (
                            200,
                            json!({"data":[{"id":"c2","name":"Second","language":"no","is_owner":true}]}),
                        )
                    } else if path.starts_with("/voices?") {
                        (
                            200,
                            json!({"data":[{"id":"c1","name":"Updated","language":"en","is_public":true}],
                            "has_more":failures != 3,"next_page":"next/page"}),
                        )
                    } else if path == "/voices/c%2Bmissing" && failures != 3 {
                        (200, json!({"name":"Looked Up"}))
                    } else if elevenlabs {
                        let name = if failures == 2 {
                            "New Eleven - description"
                        } else {
                            "Eleven Name - description"
                        };
                        (200, json!({"name": name}))
                    } else {
                        (404, json!({}))
                    };
                    let body = body.to_string();
                    write!(stream, "HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
                }
            }));
            server
        }
    }

    impl Drop for Catalogue {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            let _ = TcpStream::connect(self.addr);
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
        }
    }

    struct Temp(PathBuf);

    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn voice_names_refresh_uses_its_private_file_and_bounded_provider_calls() {
        let server = Catalogue::start();
        let root = Temp(crate::once::a_place("voice-names-test").unwrap());
        let path = root.0.join("voice-names.json");
        let desk = Desk::new(path.clone());
        assert!(desk.start_due());

        let initial = br#"{ "cartesia": {"c1":"Old name","retired":"Preserve old voice"}, "elevenlabs": {"old":"Keep this too"} }"#;
        fs::write(&path, initial).unwrap();
        desk.read().unwrap();
        assert!(!desk.start_due());
        let stale =
            SystemTime::now() - voice_names::REFRESH_EVERY - std::time::Duration::from_secs(5);
        File::open(&path).unwrap().set_modified(stale).unwrap();
        desk.read().unwrap();
        assert!(desk.start_due());
        assert_eq!(desk.snapshot().1, Some(digest(initial)));
        let mut used = Used::new();
        for id in ["c1", "c+missing"] {
            record(&mut used, CARTESIA, id.into(), "Fixture");
        }
        for id in 0..109 {
            record(&mut used, ELEVENLABS, format!("e{id:03}"), "");
        }
        record(
            &mut used,
            ELEVENLABS,
            "01234567-0123-0123-0123-012345678901".into(),
            "",
        );
        record(
            &mut used,
            ELEVENLABS,
            r#"{"cartesia":"fixture"}"#.into(),
            "",
        );
        desk.refresh(&Sources::default(), &used).unwrap();
        assert!(server.asked.lock().unwrap().is_empty());
        assert_eq!(fs::read(&path).unwrap(), initial);
        assert!(desk.start_due());

        let endpoint = format!("http://{}", server.addr);
        let sources = Sources {
            cartesia: Some(Provider {
                key: "fixture".into(),
                endpoint: format!("{endpoint}/voices"),
            }),
            elevenlabs: Some(Provider {
                key: "fixture".into(),
                endpoint,
            }),
        };
        {
            let _running = desk.refreshing.lock().unwrap();
            desk.refresh(&sources, &used).unwrap();
            assert!(server.asked.lock().unwrap().is_empty());
        }
        desk.refresh(&sources, &used).unwrap();
        let sent = server.asked.lock().unwrap().clone();
        assert_eq!(sent.len(), 103);
        assert!(sent[0].starts_with("/voices?") && !sent[0].contains("language="));
        assert!(sent[1].contains("starting_after=next%2Fpage"));
        assert_eq!(sent[2], "/voices/c%2Bmissing");
        assert_eq!(sent[3], "/v1/voices/e000");
        assert_eq!(sent[102], "/v1/voices/e099");
        let (names, signature) = desk.snapshot();
        assert_eq!(names.names[CARTESIA]["retired"], "Preserve old voice");
        assert_eq!(names.names[CARTESIA]["c1"], "Updated");
        assert_eq!(names.names[CARTESIA]["c2"], "Second");
        assert_eq!(names.names[CARTESIA]["c+missing"], "Looked Up");
        assert_eq!(
            names.catalogue[CARTESIA]["c1"],
            CatalogueVoice {
                language: "en".into(),
                public: true,
                owner: false
            }
        );
        assert_eq!(names.names[ELEVENLABS]["old"], "Keep this too");
        assert_eq!(names.names[ELEVENLABS]["e000"], "Eleven Name");
        assert_ne!(signature, Some(digest(initial)));
        assert!(!desk.start_due());

        use std::os::unix::fs::MetadataExt as _;
        let after = fs::read(&path).unwrap();
        assert_eq!(signature, Some(digest(&after)));
        let inode = fs::metadata(&path).unwrap().ino();
        File::open(&path).unwrap().set_modified(stale).unwrap();
        desk.refresh(&sources, &used).unwrap();
        assert_eq!(desk.snapshot(), (names.clone(), signature.clone()));
        assert_eq!(fs::read(&path).unwrap(), after);
        assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
        assert!(!desk.start_due());

        server.failures.store(1, Ordering::SeqCst);
        desk.refresh(&sources, &used).unwrap();
        assert_eq!(desk.snapshot(), (names.clone(), signature.clone()));
        assert_eq!(fs::read(&path).unwrap(), after);
        assert_eq!(server.asked.lock().unwrap().len(), 307);

        server.failures.store(2, Ordering::SeqCst);
        desk.refresh(&sources, &used).unwrap();
        let (after_partial, changed) = desk.snapshot();
        assert_eq!(after_partial.names[CARTESIA], names.names[CARTESIA]);
        assert_eq!(after_partial.names[ELEVENLABS]["e000"], "New Eleven");
        assert_eq!(after_partial.names[ELEVENLABS]["old"], "Keep this too");
        assert_ne!(changed, signature);
        assert_eq!(server.asked.lock().unwrap().len(), 408);

        server.failures.store(3, Ordering::SeqCst);
        desk.refresh(&sources, &used).unwrap();
        let (deleted, digest) = desk.snapshot();
        assert_eq!(deleted.gone[CARTESIA], BTreeSet::from(["c+missing".into()]));
        assert!(!deleted.names[CARTESIA].contains_key("c+missing"));
        assert_eq!(deleted.names[CARTESIA]["c2"], "Second");
        assert!(!deleted.catalogue[CARTESIA].contains_key("c2"));
        desk.refresh(&sources, &used).unwrap();
        assert_eq!(desk.snapshot(), (deleted, digest));
    }
}
