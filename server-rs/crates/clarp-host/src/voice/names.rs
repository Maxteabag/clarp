//! Voice labels are fetched and persisted off the profile and catalogue read paths.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::SystemTime;

use clarp_kernel::said::{self, CARTESIA, ELEVENLABS};
use clarp_kernel::voice_names::{self, CatalogueVoice, Refusal, Snapshot};
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
                Snapshot::from_file(&Value::Null)
            }
        };
        self.publish(Stored {
            stamp,
            snapshot,
            hash,
        });
        Ok(())
    }

    fn start_due(&self, sources: &Sources) -> bool {
        let current = self.current.lock().unwrap_or_else(PoisonError::into_inner);
        let age = current
            .stamp
            .map(|(modified, _)| modified.elapsed().unwrap_or_default());
        voice_names::start_due(age, current.snapshot.format)
            || sources.key_changed(&current.snapshot)
    }

    /// Return whether the catalogue needs a retry.
    fn refresh(&self, sources: &Sources, used: &Used, retry: bool) -> io::Result<bool> {
        let Ok(_refresh) = self.refreshing.try_lock() else {
            return Ok(false);
        };
        self.read()?;
        let (before, old_hash, modified) = {
            let current = self.current.lock().unwrap_or_else(PoisonError::into_inner);
            (
                current.snapshot.clone(),
                current.hash.clone(),
                current.stamp.map(|(time, _)| time),
            )
        };
        let (fetched, failed) = sources.fetch(&before, used, retry);
        let mut snapshot = before.clone();
        let Some(newly_gone) = snapshot.refresh(fetched, sources.has_key(), failed) else {
            return Ok(failed);
        };
        let hash = if old_hash.is_some() && snapshot == before {
            if failed {
                return Ok(failed);
            }
            File::open(&self.path)?.set_modified(SystemTime::now())?;
            old_hash
        } else {
            let raw = serde_json::to_vec(&snapshot).map_err(io::Error::other)?;
            self.replace(&raw, modified.filter(|_| failed))?;
            Some(digest(&raw))
        };
        let meta = fs::metadata(&self.path)?;
        let refused: Vec<_> = snapshot
            .refused
            .iter()
            .filter(|(provider, mark)| before.refused.get(*provider) != Some(*mark))
            .map(|(provider, mark)| (provider.clone(), mark.status))
            .collect();
        self.publish(Stored {
            stamp: Some((meta.modified()?, meta.len())),
            snapshot,
            hash,
        });
        for (provider, status) in refused {
            eprintln!("voiceNamesKeyRefused provider={provider} status={status}");
        }
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
        Ok(failed)
    }

    fn replace(&self, raw: &[u8], modified: Option<SystemTime>) -> io::Result<()> {
        let tmp = self.path.with_extension(format!("{}.tmp", ids::hex(8)));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&tmp)?;
        let written = file
            .write_all(raw)
            .and_then(|()| modified.map_or(Ok(()), |time| file.set_modified(time)))
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
    fn has_key(&self) -> bool {
        self.cartesia.is_some() || self.elevenlabs.is_some()
    }

    fn key_changed(&self, current: &Snapshot) -> bool {
        [(CARTESIA, &self.cartesia), (ELEVENLABS, &self.elevenlabs)]
            .into_iter()
            .any(|(provider, source)| {
                source.as_ref().is_some_and(|source| {
                    current.key_changed(provider, &voice_names::key_mark(&source.key))
                })
            })
    }

    fn fetch(&self, current: &Snapshot, used: &Used, retry: bool) -> (Snapshot, bool) {
        let mut fetched = Snapshot::default();
        let mut failed = false;
        let ids = |provider: &str| {
            used.get(provider)
                .into_iter()
                .flat_map(|voices| voices.keys().cloned())
        };
        if let Some(source) = &self.cartesia
            && !current.key_refused(CARTESIA, &voice_names::key_mark(&source.key))
        {
            match clarp_speech::cartesia::all_voices(&source.key, &source.endpoint) {
                Ok(rows) => {
                    let today: BTreeMap<_, _> = rows
                        .iter()
                        .filter_map(|row| {
                            let id = row["id"].as_str().filter(|id| !id.is_empty())?;
                            Some((id.to_owned(), CatalogueVoice::from_cartesia(row)))
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
                    if !retry {
                        let missing = ids(CARTESIA).filter(|id| !today.contains_key(id)).collect();
                        let gone = lookup(&missing, &mut listed, CARTESIA, |id| {
                            clarp_speech::cartesia::voice_by_id(&source.key, &source.endpoint, id)
                                .map(|row| row["name"].as_str().unwrap_or("").trim().to_owned())
                        });
                        if let Ok(gone) = gone {
                            fetched.gone.insert(CARTESIA.into(), gone);
                        }
                    }
                    fetched.names.insert(CARTESIA.into(), listed);
                    fetched.catalogue.insert(CARTESIA.into(), today);
                }
                Err(LookupError::Http(status)) if voice_names::refuses_key(CARTESIA, status) => {
                    fetched
                        .refused
                        .insert(CARTESIA.into(), source.refusal(status));
                }
                Err(why) => {
                    eprintln!("voiceNamesFetchFail {CARTESIA}: {why}");
                    failed = true;
                }
            }
        }
        let eleven_ids: BTreeSet<_> = ids(ELEVENLABS)
            .filter(|id| clarp_kernel::voice_profile::elevenlabs_id_usable(Some(id)))
            .collect();
        if !retry
            && let Some(source) = &self.elevenlabs
            && !eleven_ids.is_empty()
            && !current.key_refused(ELEVENLABS, &voice_names::key_mark(&source.key))
        {
            let mut found = ProviderNames::new();
            let gone = lookup(&eleven_ids, &mut found, ELEVENLABS, |id| {
                clarp_speech::eleven_voice::voice_by_id(&source.key, &source.endpoint, id)
            });
            match gone {
                Ok(gone) => {
                    fetched.names.insert(ELEVENLABS.into(), found);
                    fetched.gone.insert(ELEVENLABS.into(), gone);
                }
                Err(status) => {
                    fetched
                        .refused
                        .insert(ELEVENLABS.into(), source.refusal(status));
                }
            }
        }
        (fetched, failed)
    }
}

impl Provider {
    fn refusal(&self, status: u16) -> Refusal {
        Refusal {
            key: voice_names::key_mark(&self.key),
            status,
        }
    }
}

fn lookup(
    used: &BTreeSet<String>,
    found: &mut ProviderNames,
    provider: &str,
    mut fetch_one: impl FnMut(&str) -> Result<String, LookupError>,
) -> Result<BTreeSet<String>, u16> {
    let mut gone = BTreeSet::new();
    for id in voice_names::lookups(used, found) {
        match fetch_one(id) {
            Ok(name) if !name.is_empty() => {
                found.insert(id.to_owned(), name);
            }
            Err(LookupError::Http(404)) => {
                gone.insert(id.to_owned());
            }
            Err(LookupError::Http(status))
                if provider == ELEVENLABS && voice_names::refuses_key(provider, status) =>
            {
                return Err(status);
            }
            Err(why) => eprintln!("voiceNamesLookupFail {provider} {id}: {why}"),
            Ok(_) => {}
        }
    }
    Ok(gone)
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
            let mut retry = false;
            let mut backoff = voice_names::Backoff::default();
            while let Some(mouth) = mouth.upgrade() {
                let result =
                    tokio::task::spawn_blocking(move || mouth.refresh_voice_names(first, retry))
                        .await;
                let failed = match result {
                    Ok(Ok(failed)) => failed,
                    Ok(Err(why)) => {
                        eprintln!("voiceNamesWorkerFail {why}");
                        false
                    }
                    Err(why) => {
                        eprintln!("voiceNamesWorkerFail {why}");
                        false
                    }
                };
                first = false;
                let (wait, retry_catalogue) = backoff.next_pass(failed);
                retry = retry_catalogue;
                tokio::time::sleep(wait).await;
            }
        });
    }

    fn refresh_voice_names(&self, first: bool, retry: bool) -> io::Result<bool> {
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
        if first {
            self.voice_names.read()?;
            if !self.voice_names.start_due(&sources) {
                return Ok(false);
            }
        }
        if retry || !sources.has_key() {
            return self.voice_names.refresh(&sources, &Used::new(), retry);
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
        self.voice_names.refresh(&sources, &used, false)
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
                    let catalogue = path.starts_with("/voices?");
                    let error = match failures {
                        1 => Some(503),
                        2 if !elevenlabs => Some(503),
                        4 if catalogue => Some(401),
                        5 if catalogue => Some(403),
                        6 if !catalogue && !elevenlabs => Some(403),
                        7 if elevenlabs => Some(401),
                        8 if elevenlabs => Some(403),
                        _ => None,
                    };
                    let (status, body) = if let Some(error) = error {
                        (error, json!({}))
                    } else if path.contains("starting_after=next%2Fpage") {
                        (
                            200,
                            json!({"data":[{"id":"c2","name":"Second","language":"no","visibility":"all"}]}),
                        )
                    } else if path.starts_with("/voices?") {
                        (
                            200,
                            json!({"data":[{"id":"c1","name":"Updated","language":"en","access":"public","is_owner":false}],
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
        assert!(desk.start_due(&Sources::default()));

        let initial = br#"{ "cartesia": {"c1":"Old name","retired":"Preserve old voice"}, "elevenlabs": {"old":"Keep this too"} }"#;
        fs::write(&path, initial).unwrap();
        desk.read().unwrap();
        assert!(desk.start_due(&Sources::default()));
        let stale =
            SystemTime::now() - voice_names::REFRESH_EVERY - std::time::Duration::from_secs(5);
        File::open(&path).unwrap().set_modified(stale).unwrap();
        desk.read().unwrap();
        assert!(desk.start_due(&Sources::default()));
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
        let no_keys_path = root.0.join("no-keys.json");
        let no_keys = Desk::new(no_keys_path.clone());
        no_keys.refresh(&Sources::default(), &used, false).unwrap();
        assert_eq!(fs::read(&no_keys_path).unwrap(), br#"{"_format":2}"#);
        assert!(!no_keys.start_due(&Sources::default()));
        fs::write(
            &no_keys_path,
            br#"{"cartesia":{"c1":"Old name"},"_catalogue":{"cartesia":{"c1":{}}}}"#,
        )
        .unwrap();
        no_keys.refresh(&Sources::default(), &used, false).unwrap();
        let (upgraded, _) = no_keys.snapshot();
        assert_eq!(upgraded.names[CARTESIA]["c1"], "Old name");
        assert!(upgraded.catalogue.is_empty());
        assert_eq!(upgraded.format, Some(2));
        assert!(!no_keys.start_due(&Sources::default()));
        File::open(&no_keys_path)
            .unwrap()
            .set_modified(stale)
            .unwrap();
        let no_keys_modified = fs::metadata(&no_keys_path).unwrap().modified().unwrap();
        no_keys.refresh(&Sources::default(), &used, false).unwrap();
        assert_eq!(
            fs::metadata(&no_keys_path).unwrap().modified().unwrap(),
            no_keys_modified
        );
        assert!(server.asked.lock().unwrap().is_empty());

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
            desk.refresh(&sources, &used, false).unwrap();
            assert!(server.asked.lock().unwrap().is_empty());
        }
        desk.refresh(&sources, &used, false).unwrap();
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
        assert!(!desk.start_due(&Sources::default()));

        use std::os::unix::fs::MetadataExt as _;
        let after = fs::read(&path).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&after).unwrap()["_format"],
            2
        );
        assert!(names.catalogue[CARTESIA]["c2"].public);
        assert!(names.catalogue[CARTESIA]["c2"].owner);
        let reopened = Desk::new(path.clone());
        reopened.read().unwrap();
        assert!(!reopened.start_due(&Sources::default()));
        assert_eq!(signature, Some(digest(&after)));
        let inode = fs::metadata(&path).unwrap().ino();
        File::open(&path).unwrap().set_modified(stale).unwrap();
        desk.refresh(&sources, &used, false).unwrap();
        assert_eq!(desk.snapshot(), (names.clone(), signature.clone()));
        assert_eq!(fs::read(&path).unwrap(), after);
        assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
        assert!(!desk.start_due(&Sources::default()));

        File::open(&path).unwrap().set_modified(stale).unwrap();
        let previous_modified = fs::metadata(&path).unwrap().modified().unwrap();
        server.failures.store(1, Ordering::SeqCst);
        assert!(desk.refresh(&sources, &used, false).unwrap());
        assert_eq!(
            fs::metadata(&path).unwrap().modified().unwrap(),
            previous_modified
        );
        assert!(desk.start_due(&sources));
        assert_eq!(desk.snapshot(), (names.clone(), signature.clone()));
        assert_eq!(fs::read(&path).unwrap(), after);
        assert_eq!(server.asked.lock().unwrap().len(), 307);

        server.failures.store(2, Ordering::SeqCst);
        assert!(desk.refresh(&sources, &used, false).unwrap());
        let (after_partial, changed) = desk.snapshot();
        assert_eq!(after_partial.names[CARTESIA], names.names[CARTESIA]);
        assert_eq!(after_partial.names[ELEVENLABS]["e000"], "New Eleven");
        assert_eq!(after_partial.names[ELEVENLABS]["old"], "Keep this too");
        assert_ne!(changed, signature);
        assert_eq!(
            fs::metadata(&path).unwrap().modified().unwrap(),
            previous_modified
        );
        assert!(desk.start_due(&sources));
        assert_eq!(server.asked.lock().unwrap().len(), 408);

        server.failures.store(3, Ordering::SeqCst);
        desk.refresh(&sources, &used, false).unwrap();
        let (deleted, digest) = desk.snapshot();
        assert_eq!(deleted.gone[CARTESIA], BTreeSet::from(["c+missing".into()]));
        assert!(!deleted.names[CARTESIA].contains_key("c+missing"));
        assert_eq!(deleted.names[CARTESIA]["c2"], "Second");
        assert!(!deleted.catalogue[CARTESIA].contains_key("c2"));
        assert!(!desk.start_due(&sources));
        desk.refresh(&sources, &used, false).unwrap();
        assert_eq!(desk.snapshot(), (deleted, digest));
    }

    #[test]
    fn voice_names_remember_refused_keys_and_retry_only_the_catalogue() {
        let server = Catalogue::start();
        let root = Temp(crate::once::a_place("voice-names-refusal").unwrap());
        let endpoint = format!("http://{}", server.addr);
        let mut sources = Sources {
            cartesia: Some(Provider {
                key: "fixture-old".into(),
                endpoint: format!("{endpoint}/voices"),
            }),
            elevenlabs: None,
        };
        let mut used = Used::new();
        record(&mut used, CARTESIA, "c+missing".into(), "");
        for id in ["e001", "e002"] {
            record(&mut used, ELEVENLABS, id.into(), "");
        }
        let asked = || std::mem::take(&mut *server.asked.lock().unwrap());
        for (mode, status, attempts) in [(4, 401, 3), (5, 403, 1)] {
            let path = root.0.join(format!("refusal-{status}.json"));
            let desk = Desk::new(path.clone());
            server.failures.store(mode, Ordering::SeqCst);
            assert!(!desk.refresh(&sources, &used, false).unwrap());
            assert_eq!(asked().len(), attempts);
            let file = fs::read_to_string(&path).unwrap();
            assert!(!file.contains("fixture-old"));
            let saved: Value = serde_json::from_str(&file).unwrap();
            assert_eq!(saved["_refused"][CARTESIA]["status"], status);
            assert_eq!(
                saved["_refused"][CARTESIA]["key"],
                voice_names::key_mark("fixture-old")
            );

            let reopened = Desk::new(path);
            reopened.read().unwrap();
            assert!(!reopened.start_due(&sources));
            assert!(!reopened.refresh(&sources, &used, false).unwrap());
            assert!(asked().is_empty());

            sources.cartesia.as_mut().unwrap().key = "fixture-new".into();
            assert!(reopened.start_due(&sources));
            server.failures.store(6, Ordering::SeqCst);
            assert!(!reopened.refresh(&sources, &used, false).unwrap());
            let calls = asked();
            assert_eq!(calls.len(), 3);
            assert_eq!(calls[2], "/voices/c%2Bmissing");
            assert!(reopened.snapshot().0.refused.is_empty());
            assert!(!reopened.start_due(&sources));
            sources.cartesia.as_mut().unwrap().key = "fixture-old".into();
        }

        sources.elevenlabs = Some(Provider {
            key: "fixture-eleven".into(),
            endpoint,
        });
        let desk = Desk::new(root.0.join("retry.json"));
        server.failures.store(2, Ordering::SeqCst);
        assert!(desk.refresh(&sources, &used, false).unwrap());
        let calls = asked();
        assert_eq!(calls.len(), 3);
        assert_eq!(&calls[1..], ["/v1/voices/e001", "/v1/voices/e002"]);
        server.failures.store(0, Ordering::SeqCst);
        assert!(!desk.refresh(&sources, &used, true).unwrap());
        let calls = asked();
        assert_eq!(calls.len(), 2);
        assert!(calls.iter().all(|path| path.starts_with("/voices?")));

        sources.cartesia = None;
        server.failures.store(7, Ordering::SeqCst);
        assert!(!desk.refresh(&sources, &used, false).unwrap());
        assert_eq!(asked(), ["/v1/voices/e001"]);
        assert_eq!(desk.snapshot().0.refused[ELEVENLABS].status, 401);
        assert!(!desk.refresh(&sources, &used, false).unwrap());
        assert!(asked().is_empty());

        sources.elevenlabs.as_mut().unwrap().key = "fixture-eleven-new".into();
        assert!(desk.start_due(&sources));
        server.failures.store(8, Ordering::SeqCst);
        assert!(!desk.refresh(&sources, &used, false).unwrap());
        assert_eq!(asked(), ["/v1/voices/e001", "/v1/voices/e002"]);
        assert!(desk.snapshot().0.refused.is_empty());
    }
}
