//! Filesystem evidence for Claude helper cells. One blocking-pool reader owns
//! the cache for a bound session. Live tool state always comes from its stream.
use crate::files::{beneath, open, open_beneath};
use clarp_kernel::helper_cells::{self, Children};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, Metadata};
use std::io::{self, BufRead, Read, Seek, SeekFrom};
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
    lines:crate::native_lines::Lines,
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
        let mut lines=std::mem::take(&mut self.lines);
        let mut total=0;
        loop {
            let bytes=input.fill_buf()?;
            if bytes.is_empty(){break;}
            let n=lines.feed(bytes,self.offset,usize::MAX,|record,_|{self.record(record);true});
            total+=n;self.offset+=n as u64;
            input.consume(n);
        }
        self.lines=lines;
        Ok(total)
    }
}
#[derive(Default)]
pub struct HelperSource {
    metas: BTreeMap<String, (Stamp, Option<String>)>,
    pub(crate) children: Children,
    parent: Parent,
    provider: ProviderReader,
    parent_path:Option<std::path::PathBuf>,
    available: bool,
    pub(crate) recovered_through: Option<u64>,
    #[cfg(test)]
    counts: [usize; 3],
    #[cfg(test)]
    io_counts:[usize;2],
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
    #[cfg(test)]
    fn scan(&mut self,root:&Path,cwd:&str,session:&str,imported:bool)->bool{
        // These tests exercise the helper cursor. The native consumer is
        // already at its own committed position, as after a caught-up visit.
        let (dev,ino,end)=boundary(root,cwd,session).unwrap_or_default();
        let mut request=ProviderRead{helpers:true,parent:imported,visit:Visit{
            native:session.into(),expected:0,bound_at:0,owner:None,final_read:None,actions:Vec::new(),eof:false,
            cursor:Cursor{device:dev,inode:ino,offset:end,..Default::default()}}};
        self.read_visit(root,cwd,&mut request,0).unwrap_or(false)
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
    fn provider_refresh_resumes_at_the_first_uncommitted_action_and_byte() {
        let f=Fixture::new();
        let mut source=HelperSource::default();
        let mut read=ProviderRead{helpers:false,parent:false,
            visit:clarp_kernel::provider_tasks::Session::default().visit("parent".into())};
        let record=|range:std::ops::Range<usize>|{
            let blocks:Vec<_>=range.map(|i|json!({"type":"tool_use","name":"Bash","id":format!("tool-{i}"),
                "input":{"run_in_background":true}})).collect();
            json!({"type":"assistant","timestamp":"2027-01-15T08:00:00Z","message":{"content":blocks}}).to_string()
        };
        let now=1_800_000_000_000;
        // The newline counts toward the record ceiling. An exact record
        // survives; one byte over is skipped even across a reader restart.
        let mut exact=record(0..1);
        exact.push_str(&" ".repeat(RECORD_LIMIT-exact.len()-1));
        exact.push('\n');
        let mut oversized=record(1000..1001);
        oversized.push_str(&" ".repeat(RECORD_LIMIT-oversized.len()+1));
        std::fs::write(&f.parent,format!("{exact}{oversized}")).unwrap();
        let mut seen=Vec::new();
        for _ in 0..(2*RECORD_LIMIT/(VISIT_BYTES as usize)+1){
            let before=source.io_counts[1];
            source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
            assert!(source.io_counts[1]-before<=VISIT_BYTES as usize);
            seen.extend(read.visit.actions.drain(..).map(|a|a.tool));
        }
        assert_eq!(seen,["tool-0"]);
        assert!(read.visit.cursor.discarding);
        let mut file=fs::OpenOptions::new().append(true).open(&f.parent).unwrap();
        writeln!(file).unwrap();
        let start=file.metadata().unwrap().len();
        let many=record(1..131);
        writeln!(file,"{many}").unwrap();
        source=HelperSource::default();
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        assert_eq!(read.visit.actions.len(),64);
        assert_eq!(read.visit.cursor.offset,start);
        assert_eq!(read.visit.cursor.action_offset,64);
        seen.extend(read.visit.actions.drain(..).map(|a|a.tool));
        // Reopen at the saved action offset. The last two actions are
        // already in memory for the following visit and cost no file read.
        source=HelperSource::default();
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        assert_eq!(read.visit.actions.len(),64);
        assert_eq!(read.visit.cursor.action_offset,128);
        seen.extend(read.visit.actions.drain(..).map(|a|a.tool));
        let bytes=source.io_counts[1];
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        seen.extend(read.visit.actions.drain(..).map(|a|a.tool));
        assert_eq!(seen,(0..131).map(|i|format!("tool-{i}")).collect::<Vec<_>>());
        assert!(read.visit.eof);
        assert_eq!(read.visit.cursor.offset,file.metadata().unwrap().len());
        let stats=source.io_counts[0];
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        assert_eq!(source.io_counts,[stats+1,bytes]);
        assert!(read.visit.actions.is_empty());
        // The final record remains incomplete across a visit. Only the
        // appended newline is read to finish it by the same reader.
        let partial=record(131..132);
        write!(file,"{partial}").unwrap();
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        assert!(read.visit.actions.is_empty());
        let bytes=source.io_counts[1];
        writeln!(file).unwrap();
        source.read_visit(&f.root,"/fixture",&mut read,now).unwrap();
        assert_eq!(read.visit.actions[0].tool,"tool-131");
        assert_eq!(source.io_counts[1]-bytes,1);
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

use clarp_kernel::provider_tasks::{Cursor,Evidence,Visit,RECORD_LIMIT};

const VISIT_BYTES:u64=256*1024;
use clarp_kernel::provider_tasks::MAX_ACTIONS as VISIT_ACTIONS;
#[derive(Default)]
struct ProviderReader{
    lines:crate::native_lines::Lines,
    consumed:u64,
    // A record may contain more actions than one journal batch. A crash
    // restarts at its beginning and skips the already committed actions.
    pending:Option<(u64,Vec<Evidence>)>,
    saved:Cursor,
}

impl ProviderReader{
    fn read(&mut self,bytes:&[u8],start:u64,project:&str,visit:&mut Visit,now:i64){
        if self.saved!=visit.cursor {
            *self=Self{consumed:visit.cursor.offset,saved:visit.cursor.clone(),..Default::default()};
            self.lines.discard=visit.cursor.discarding;
        }
        if let Some((end,actions))=&self.pending{
            let n=visit.cursor.action_offset.min(actions.len());
            visit.actions.extend(actions.iter().skip(n).take(VISIT_ACTIONS).cloned());
            visit.cursor.action_offset=n+visit.actions.len();
            if visit.cursor.action_offset==actions.len(){
                visit.cursor.offset=*end;visit.cursor.action_offset=0;self.pending=None;
            }
        }
        if self.pending.is_none() && visit.actions.len()<VISIT_ACTIONS && self.consumed>=start{
            let from=(self.consumed-start).min(bytes.len() as u64) as usize;
            let used=self.lines.feed(&bytes[from..],start+from as u64,RECORD_LIMIT,|line,end|{
                let actions=serde_json::from_slice(line).ok().map_or_else(Vec::new,|value|clarp_kernel::provider_tasks::parse_actions(&visit.native,project,&value,now));
                let n=visit.cursor.action_offset.min(actions.len());
                let take=(actions.len()-n).min(VISIT_ACTIONS-visit.actions.len());
                visit.actions.extend(actions.iter().skip(n).take(take).cloned());
                if n+take<actions.len(){
                    self.pending=Some((end,actions));visit.cursor.action_offset=n+take;visit.cursor.offset=end - line.len() as u64;return false
                }
                visit.cursor.offset=end;visit.cursor.action_offset=0;
                visit.actions.len()<VISIT_ACTIONS
            });
            self.consumed+=used as u64;
            if self.lines.discard || self.lines.fragment.is_empty() && self.pending.is_none(){visit.cursor.offset=self.consumed;}
            visit.cursor.discarding=self.lines.discard;
        }
        self.saved=visit.cursor.clone();
    }
}
pub(crate) struct ProviderRead {
    pub visit:Visit,

    pub helpers:bool,
    pub parent:bool,
}
impl HelperSource{
    pub(crate) fn has_uncommitted_bytes(&self,cursor:&Cursor)->bool{
        self.provider.consumed>cursor.offset || self.provider.pending.is_some()
    }

    /// The on-disk position may lag the in-memory reader by a partial line.
    /// Read only the missing window and hand its shared slice to each cursor.
    pub(crate) fn read_visit(&mut self,root:&Path,cwd:&str,request:&mut ProviderRead,now:i64)->io::Result<bool>{
        let v=&mut request.visit;
        if !clarp_kernel::native_session_name(&v.native){return Err(io::Error::other("invalid native session"));}
        let path=self.parent_path.get_or_insert_with(||root.join(clarp_backend::past::claude_project(cwd)).join(format!("{}.jsonl",v.native))).clone();
        #[cfg(test)] {self.io_counts[0]+=1;}
        let meta=match std::fs::symlink_metadata(&path) {
            Ok(meta) if meta.is_file()=>meta,
            Ok(_)=>return Err(io::Error::other("provider transcript is not a regular file")),
            Err(e) if e.kind()==io::ErrorKind::NotFound=>{
                v.eof=true;let changed=self.available;self.available=false;self.children.clear();self.metas.clear();return Ok(changed);
            }
            Err(e)=>return Err(e),
        };
        let stamp=Stamp::from(&meta);
        let changed=request.helpers && (self.discover(root,&path)||!self.available);self.available=true;
        if v.final_read.as_ref().is_some_and(|end|end.device!=stamp.device || end.inode!=stamp.inode){
            v.eof=true;return Ok(changed)
        }
        if v.cursor.device!=stamp.device || v.cursor.inode!=stamp.inode || v.cursor.offset>stamp.bytes ||
            self.provider.saved==v.cursor && self.provider.consumed>stamp.bytes{
            v.cursor=Cursor{device:stamp.device,inode:stamp.inode,..Default::default()};self.provider=ProviderReader::default();
        }
        if self.parent.stamp.is_some_and(|old|old.device!=stamp.device||old.inode!=stamp.inode||self.parent.offset>stamp.bytes||old.bytes==stamp.bytes && old!=stamp){
            self.parent=Parent{changed:true,..Default::default()};
        }
        let end=v.final_read.as_ref().map_or(stamp.bytes,|bound|bound.end.min(stamp.bytes));
        let next=if self.provider.saved==v.cursor{self.provider.consumed}else{v.cursor.offset};
        let start=if request.parent {next.min(self.parent.offset)}else{next};
        let mut bytes=Vec::new();
        if start<end {
            let relative=path.strip_prefix(root).ok().and_then(|p|p.to_str()).ok_or_else(||io::Error::other("invalid provider path"))?;
            let (_,mut file)=open_beneath(root,relative,false).map_err(|_|io::Error::other("provider parent unavailable"))?;
            let m=file.metadata()?;
            if !m.is_file() || m.dev()!=stamp.device || m.ino()!=stamp.inode {return Err(io::Error::other("provider parent changed"))}
            file.seek(SeekFrom::Start(start))?;file.take((end-start).min(VISIT_BYTES)).read_to_end(&mut bytes)?;
            // Replacement or truncation during the read invalidates the evidence.
            let latest=std::fs::symlink_metadata(&path)?;
            if latest.dev()!=stamp.device || latest.ino()!=stamp.inode || latest.len()<start+bytes.len() as u64 {
                return Err(io::Error::other("provider parent changed while read"));
            }
            #[cfg(test)] {self.io_counts[1]+=bytes.len();}
        }
        self.parent.changed=false;
        if request.parent && self.parent.offset>=start && self.parent.offset<start+bytes.len() as u64{
            let at=(self.parent.offset-start) as usize;
            let _consumed=self.parent.consume(&bytes[at..])?;
            #[cfg(test)] {self.counts[2]+=_consumed;}
            self.parent.stamp=Some(stamp);
        }
        let project=path.parent().and_then(Path::file_name).and_then(|s|s.to_str()).unwrap_or("");
        // If the helper reader trails the durable job cursor by a full
        // window, it alone consumes this window; don't move the job cursor.
        if next<=start+bytes.len() as u64 {
            self.provider.read(&bytes,start,project,v,now);
            if !clarp_kernel::provider_tasks::valid_id(&v.native){v.actions.clear();}
        }
        v.eof=self.provider.pending.is_none() && self.provider.consumed>=end && (!request.parent||self.parent.offset>=end);
        if v.final_read.is_some() && v.eof {v.cursor.offset=end;v.cursor.action_offset=0;}
        Ok(changed||self.parent.changed)
    }
}

pub(crate) fn boundary(root:&Path,cwd:&str,session:&str)->io::Result<(u64,u64,u64)>{
    if !clarp_kernel::native_session_name(session){return Err(io::Error::other("invalid native session"));}
    let path=root.join(clarp_backend::past::claude_project(cwd)).join(format!("{session}.jsonl"));
    match std::fs::symlink_metadata(path){
        Ok(m) if m.is_file()=>Ok((m.dev(),m.ino(),m.len())),
        Ok(_)=>Err(io::Error::other("provider parent is not a file")),
        Err(e) if e.kind()==io::ErrorKind::NotFound => Ok((0,0,0)),
        Err(e)=>Err(e),
    }
}
