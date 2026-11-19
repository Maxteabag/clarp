//! Filesystem evidence for Claude helper cells. One blocking-pool reader owns
//! the cache for a bound session. Live tool state always comes from its stream.
use crate::files::{beneath, open, open_beneath};
use clarp_kernel::helper_cells::{self, Children};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, Metadata};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::time::{Duration, Instant};

const META_BYTES: u64 = 64 * 1024;

#[derive(PartialEq, Eq, Clone, Copy)]
struct Stamp {
    device: u64,
    inode: u64,
    bytes: u64,
    seconds: i64,
    nanos: i64,
}
impl From<&Metadata> for Stamp {
    fn from(m: &Metadata) -> Self {
        Self {
            device: m.dev(),
            inode: m.ino(),
            bytes: m.len(),
            seconds: m.ctime(),
            nanos: m.ctime_nsec(),
        }
    }
}
#[derive(Default)]
struct Parent {
    stamp: Option<Stamp>,
    offset: u64,
    fragment: Vec<u8>,
    cells: BTreeMap<String, Value>,
    meta_ids: BTreeSet<String>,
    changed: bool,
}
impl Parent {
    fn record(&mut self, line: &[u8]) {
        let Ok(r) = serde_json::from_slice::<Value>(line) else {
            return;
        };
        let kind = r["type"].as_str().unwrap_or("");
        if kind == "queue-operation" {
            if let Some(text) = r["content"].as_str() {
                self.notify(text);
            }
            return;
        }
        if !matches!(kind, "assistant" | "user") {
            return;
        }
        if r["isMeta"] == true {
            if let Some(id) = r["uuid"].as_str() {
                self.meta_ids.insert(id.to_owned());
            }
            return;
        }
        if kind == "assistant"
            && (clarp_backend::history::made_up(&r)
                || r["parentUuid"]
                    .as_str()
                    .is_some_and(|id| self.meta_ids.contains(id)))
        {
            return;
        }
        let content = &r["message"]["content"];
        let mut texts = Vec::new();
        for b in content.as_array().into_iter().flatten() {
            match b["type"].as_str() {
                Some("tool_use")
                    if b["name"].as_str().is_some_and(helper_cells::is_helper)
                        && clarp_kernel::python::truthy(&b["id"]) =>
                {
                    let id = b["id"]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| clarp_kernel::python::repr(&b["id"]));
                    self.changed = true;
                    self.cells
                        .insert(id.clone(), helper_cells::started(&id, &b["input"]));
                }
                Some("tool_result") => {
                    if let Some(c) = b["tool_use_id"]
                        .as_str()
                        .and_then(|id| self.cells.get_mut(id))
                    {
                        self.changed = true;
                        helper_cells::returned(c, b, &r["toolUseResult"]);
                    }
                }
                Some("text") if kind == "user" => texts.extend(b["text"].as_str()),
                _ => {}
            }
        }
        if kind == "user" {
            if let Some(text) = content.as_str() {
                self.notify(text)
            } else if !texts.is_empty() {
                self.notify(&texts.join("\n"));
            }
        }
    }
    fn notify(&mut self, text: &str) {
        helper_cells::notifications(text, |id, status, agent, result| {
            if let Some(cell) = self.cells.get_mut(id) {
                self.changed = true;
                helper_cells::notified(cell, status, agent, result);
            }
        });
    }
    fn consume(&mut self, mut input: impl BufRead) -> io::Result<usize> {
        let mut fragment = std::mem::take(&mut self.fragment);
        let mut total = 0;
        loop {
            let n = input.read_until(b'\n', &mut fragment)?;
            total += n;
            if n == 0 {
                break;
            }
            if fragment.last() == Some(&b'\n') {
                self.record(&fragment);
                fragment.clear();
            }
        }
        self.fragment = fragment;
        self.offset += total as u64;
        Ok(total)
    }
}
#[derive(Default)]
pub struct HelperSource {
    metas: BTreeMap<String, (Stamp, Option<String>)>,
    pub(crate) children: Children,
    parent: Parent,
    available: bool,
    pub(crate) recovered_through: Option<u64>,
    #[cfg(test)]
    counts: [usize; 3],
}
impl HelperSource {
    pub(crate) fn seed(&mut self, file: &File, text: &str) -> io::Result<()> {
        let stamp = Stamp::from(&file.metadata()?);
        self.parent.consume(text.as_bytes())?;
        self.parent.stamp = Some(stamp);
        self.available = true;
        Ok(())
    }
    fn directory(&mut self, root: &Path, parent: &Path) -> Children {
        let mut found = Children::new();
        let Some(dir) = parent
            .parent()
            .zip(parent.file_stem())
            .map(|(p, s)| p.join(s).join("subagents"))
        else {
            return found;
        };
        let Some(relative) = dir.strip_prefix(root).ok().and_then(|p| p.to_str()) else {
            return found;
        };
        let Ok((_, handle)) = open_beneath(root, relative, true) else {
            self.metas.clear();
            return found;
        };
        let Ok(entries) = std::fs::read_dir(format!("/proc/self/fd/{}", handle.as_raw_fd())) else {
            return found;
        };
        #[cfg(test)]
        {
            self.counts[0] += 1;
        }
        let mut metas = Vec::new();
        for entry in entries.flatten() {
            if !entry.file_type().is_ok_and(|t| t.is_file()) {
                continue;
            }
            let Ok(name) = entry.file_name().into_string() else {
                continue;
            };
            let Some(suffix) = name.strip_prefix("agent-") else {
                continue;
            };
            if let Some(id) = suffix.strip_suffix(".jsonl")
                && clarp_kernel::native_session_name(id)
            {
                found.insert(id.into(), dir.join(name).display().to_string());
            } else if let Some(id) = suffix.strip_suffix(".meta.json")
                && clarp_kernel::native_session_name(id)
                && let Ok(m) = entry.metadata()
            {
                metas.push((id.to_owned(), name, Stamp::from(&m)));
            }
        }
        let mut old = std::mem::take(&mut self.metas);
        for (id, name, stamp) in metas {
            let Some(path) = found.get(&id).cloned() else {
                continue;
            };
            let call = if let Some((_, call)) = old.remove(&name).filter(|(was, _)| *was == stamp) {
                call
            } else if stamp.bytes > META_BYTES {
                None
            } else {
                open(&beneath(&handle, &name), false).ok().and_then(|file| {
                    let m = file.metadata().ok()?;
                    if !m.is_file() || Stamp::from(&m) != stamp {
                        return None;
                    }
                    #[cfg(test)]
                    {
                        self.counts[1] += 1;
                    }
                    let meta: Value = serde_json::from_reader(file.take(META_BYTES)).ok()?;
                    meta["toolUseId"]
                        .as_str()
                        .filter(|id| !id.is_empty())
                        .map(str::to_owned)
                })
            };
            if let Some(call) = &call {
                found.entry(call.clone()).or_insert(path);
            }
            self.metas.insert(name, (stamp, call));
        }
        found
    }
    pub(crate) fn discover(&mut self, root: &Path, parent: &Path) -> bool {
        let found = self.directory(root, parent);
        let changed = found != self.children;
        self.children = found;
        changed
    }
    fn read_parent(&mut self, root: &Path, path: &Path) -> io::Result<bool> {
        // An unchanged imported transcript needs only a stat; no parent
        // handle is opened on this path. Recheck identity on the opened file.
        let seen = Stamp::from(&std::fs::symlink_metadata(path)?);
        if self.parent.stamp == Some(seen) && self.parent.offset == seen.bytes {
            return Ok(false);
        }
        let relative = path
            .strip_prefix(root)
            .ok()
            .and_then(|p| p.to_str())
            .ok_or_else(|| io::Error::other("invalid parent path"))?;
        let (_, mut file) = open_beneath(root, relative, false)
            .map_err(|_| io::Error::other("parent unavailable"))?;
        let meta = file.metadata()?;
        if !meta.is_file() {
            return Err(io::Error::other("parent is not a file"));
        }
        let stamp = Stamp::from(&meta);
        if self.parent.stamp == Some(stamp) && self.parent.offset == stamp.bytes {
            return Ok(false);
        }
        self.parent.changed = false;
        if !self.parent.stamp.is_some_and(|old| {
            old.device == stamp.device
                && old.inode == stamp.inode
                && (stamp.bytes > old.bytes || old == stamp)
                && self.parent.offset <= stamp.bytes
        }) {
            self.parent = Parent {
                changed: !self.parent.cells.is_empty(),
                ..Default::default()
            };
        }
        file.seek(SeekFrom::Start(self.parent.offset))?;
        let data = file.take(stamp.bytes - self.parent.offset);
        let _bytes = match self.parent.consume(BufReader::new(data)) {
            Ok(bytes) => bytes,
            Err(error) => {
                self.parent.stamp = None;
                return Err(error);
            }
        };
        #[cfg(test)]
        {
            self.counts[2] += _bytes;
        }
        self.parent.stamp = Some(stamp);
        Ok(self.parent.changed)
    }
    pub(crate) fn scan(&mut self, root: &Path, cwd: &str, session: &str, imported: bool) -> bool {
        let Some((root, path)) = root.canonicalize().ok().and_then(|r| {
            clarp_backend::past::claude_transcript(&r, cwd, session, false).map(|p| (r, p))
        }) else {
            let changed = !self.children.is_empty() || self.available;
            self.children.clear();
            self.metas.clear();
            self.available = false;
            return changed;
        };
        let changed = self.discover(&root, &path) || !self.available;
        self.available = true;
        let parsed = imported && self.read_parent(&root, &path).unwrap_or(false);
        changed || parsed
    }
    pub(crate) fn apply(&self, cell: &mut Value, imported: bool, turn: Option<u64>) {
        let id = cell["id"].as_str().unwrap_or("").to_owned();
        let missed = turn
            .zip(self.recovered_through)
            .is_some_and(|(t, last)| t <= last)
            && cell["status"] == "running";
        if (imported || missed)
            && self.available
            && let Some(parent) = self.parent.cells.get(&id)
            && (imported || parent["status"] != "running")
        {
            *cell = helper_cells::shown(parent, &self.children);
            return;
        }
        let child = self
            .children
            .get(&id)
            .or_else(|| self.children.get(cell["agent_id"].as_str().unwrap_or("")));
        if let Some(path) = child
            && cell["agent_id"] == ""
            && let Some(agent) = Path::new(path)
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|s| s.strip_prefix("agent-"))
        {
            cell["agent_id"] = json!(agent);
        }
        cell["transcript_path"] = json!(child.map(String::as_str).unwrap_or(""));
        if let Some(lines) = cell["lines"].as_array_mut() {
            lines.retain(|line| line["label"] != "Transcript");
            if let Some(path) = child {
                lines.push(json!({"label":"Transcript","text":path,"kind":"muted"}));
            }
        }
    }
}

/// A live turn may refresh once per interval. Its transition to idle has
/// exactly one outstanding final refresh, including when a scan is in flight.
#[derive(Default)]
pub struct RefreshClock {
    live: bool,
    final_due: bool,
    checked: Option<Instant>,
}
impl RefreshClock {
    pub fn due(&mut self, live: bool, now: Instant) -> Option<Instant> {
        if self.live && !live {
            self.final_due = true;
        }
        self.live = live;
        if self.final_due {
            return Some(now);
        }
        live.then(|| self.checked.map_or(now, |t| t + Duration::from_secs(1)))
    }
    pub fn started(&mut self, now: Instant) {
        self.final_due = false;
        self.checked = Some(now);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::{fs, io::Write, path::PathBuf};

    struct Fixture {
        root: PathBuf,
        parent: PathBuf,
        children: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let root =
                std::env::temp_dir().join(format!("clarp-helper-test-{}", crate::ids::hex(12)));
            let project = root.join(clarp_backend::past::claude_project("/fixture"));
            let children = project.join("parent/subagents");
            fs::create_dir_all(&children).unwrap();
            let parent = project.join("parent.jsonl");
            fs::write(&parent, b"").unwrap();
            Self {
                root,
                parent,
                children,
            }
        }
        fn poll(
            &self,
            source: &mut HelperSource,
            clock: &mut RefreshClock,
            now: Instant,
            live: bool,
        ) {
            if clock.due(live, now).is_some_and(|at| at <= now) {
                clock.started(now);
                source.scan(&self.root, "/fixture", "parent", false);
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn helper_refresh_idle_reads_nothing_and_end_reads_once() {
        let f = Fixture::new();
        let mut source = HelperSource::default();
        let mut clock = RefreshClock::default();
        let start = Instant::now();
        for s in [0, 1, 60] {
            f.poll(
                &mut source,
                &mut clock,
                start + Duration::from_secs(s),
                false,
            );
        }
        assert_eq!(source.counts, [0, 0, 0]);
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(61),
            true,
        );
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(62),
            false,
        );
        assert_eq!(source.counts, [2, 0, 0]);
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(120),
            false,
        );
        assert_eq!(source.counts, [2, 0, 0]);
    }

    #[test]
    fn helper_refresh_live_reads_only_changed_sidecars() {
        let f = Fixture::new();
        let mut source = HelperSource::default();
        let mut clock = RefreshClock::default();
        let start = Instant::now();
        let large = json!({"message":"a".repeat(3*1024*1024)});
        fs::write(&f.parent, large.to_string()).unwrap();
        for tick in 0..2 {
            f.poll(
                &mut source,
                &mut clock,
                start + Duration::from_secs(tick),
                true,
            );
        }
        assert_eq!(source.counts, [2, 0, 0]);
        let child = f.children.join("agent-fixture.jsonl");
        fs::write(&child, "synthetic").unwrap();
        let meta = f.children.join("agent-fixture.meta.json");
        fs::write(&meta, r#"{"toolUseId":"call"}"#).unwrap();
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(2),
            true,
        );
        assert_eq!(source.counts, [3, 1, 0]);
        assert_eq!(source.children["call"], child.display().to_string());
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(3),
            true,
        );
        assert_eq!(source.counts, [4, 1, 0]);
        let replacement = f.children.join("replacement");
        fs::write(&replacement, r#"{"toolUseId":"next"}"#).unwrap();
        fs::rename(&replacement, &meta).unwrap();
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(4),
            true,
        );
        assert_eq!(source.counts, [5, 2, 0]);
        assert!(!source.children.contains_key("call"));
        assert!(source.children.contains_key("next"));
        fs::write(
            &meta,
            format!(
                "{}{}",
                r#"{"toolUseId":"too-big"}"#,
                " ".repeat(META_BYTES as usize)
            ),
        )
        .unwrap();
        f.poll(
            &mut source,
            &mut clock,
            start + Duration::from_secs(5),
            true,
        );
        assert_eq!(source.counts, [6, 2, 0]);
        assert!(!source.children.contains_key("too-big"));
    }

    #[test]
    fn helper_refresh_import_reads_only_appended_bytes() {
        let f = Fixture::new();
        let call = json!({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","id":"alpha","input":{"description":"inspect"}}]}});
        let result = json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"alpha","content":"found"}]}});
        let text = format!(
            "{}\n{call}\n",
            json!({"type":"other","data":"x".repeat(3*1024*1024)})
        );
        fs::write(&f.parent, &text).unwrap();
        let mut source = HelperSource::default();
        source.seed(&File::open(&f.parent).unwrap(), &text).unwrap();
        let data = format!("{result}\n");
        let mid = data.len() / 2;
        let mut file = fs::OpenOptions::new().append(true).open(&f.parent).unwrap();
        file.write_all(&data.as_bytes()[..mid]).unwrap();
        source.scan(&f.root, "/fixture", "parent", true);
        assert_eq!(source.counts, [1, 0, mid]);
        assert_eq!(source.parent.cells["alpha"]["status"], "running");
        file.write_all(&data.as_bytes()[mid..]).unwrap();
        source.scan(&f.root, "/fixture", "parent", true);
        assert_eq!(source.counts, [2, 0, data.len()]);
        assert_eq!(source.parent.cells["alpha"]["status"], "ok");
        source.scan(&f.root, "/fixture", "parent", true);
        assert_eq!(source.counts, [3, 0, data.len()]);
        let replacement = f.parent.with_extension("new");
        let short = format!("{call}\n");
        fs::write(&replacement, &short).unwrap();
        fs::rename(replacement, &f.parent).unwrap();
        source.scan(&f.root, "/fixture", "parent", true);
        assert_eq!(source.counts[2], data.len() + short.len());
        assert_eq!(source.parent.cells["alpha"]["status"], "running");
        fs::write(&f.parent, b"{}\n").unwrap();
        source.scan(&f.root, "/fixture", "parent", true);
        assert_eq!(source.counts[2], data.len() + short.len() + 3);
        assert!(source.parent.cells.is_empty());
    }
}
