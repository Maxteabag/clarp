//! A live call can opt in to keeping text and receipts. One worker owns its
//! files; disk latency must not stall either end of the audio socket.
use crate::{edge::App,wire};
use clarp_kernel::{live_call::transcript, rings::Joined};
use serde_json::{Value,json};
use std::fs::{self,DirBuilder,OpenOptions};
use std::io::{self,Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt,OpenOptionsExt};
use std::path::{Path,PathBuf};
use std::sync::mpsc;
use std::time::{Duration,SystemTime};

pub(super) struct Transcript {
    entries:mpsc::Sender<Value>,
    finished:tokio::task::JoinHandle<()>,
}

impl Transcript {
    pub fn start(app:&App,joined:&Joined)->Option<Self>{
        let config=&joined.snapshot["live_tools"];
        if config["save_transcript"]!=true{return None;}
        let days=config["transcript_keep_days"].as_u64().filter(|d|(1..=365).contains(d));
        let name=transcript::name(&joined.call_id);
        let meta=json!({"call_id":joined.call_id,"principal":joined.principal,"agent_session":joined.snapshot["agent"]["session"],"agent":joined.snapshot["profile"]["display_name"]});
        let root=app.config.data_dir.join("agent-live/transcripts");
        let clock=app.clock.clone();
        let started=wire::iso(clock.now_ms());
        let (entries,rx)=mpsc::channel();
        let finished=tokio::task::spawn_blocking(move ||{
            if let Err(err)=write_call(&root,&name,days,&started,&meta,rx,||wire::iso(clock.now_ms())){
                eprintln!("clarp-host: live call transcript: {}",err.kind());
            }
        });
        Some(Self{entries,finished})
    }
    pub fn add(&self,kind:&str,mut fields:Value,now:i64){
        fields["at"]=json!(wire::iso(now));fields["kind"]=json!(kind);
        let _=self.entries.send(fields);
    }
    pub async fn finish(self){
        drop(self.entries);
        let _=self.finished.await;
    }
}

fn write_call(root:&Path,name:&str,days:Option<u64>,started:&str,meta:&Value,rx:mpsc::Receiver<Value>,ended:impl FnOnce()->String)->io::Result<()>{
    DirBuilder::new().recursive(true).mode(0o700).create(root)?;
    let directory=OpenOptions::new().read(true).custom_flags(nix::libc::O_DIRECTORY|nix::libc::O_NOFOLLOW).open(root)?;
    let root=PathBuf::from(format!("/proc/self/fd/{}",directory.as_raw_fd()));
    if let Some(cutoff)=days.and_then(|d|SystemTime::now().checked_sub(Duration::from_secs(d*86400))){
        for item in fs::read_dir(&root)?.flatten(){
            if item.file_name().to_string_lossy().contains(".json") && let Ok(st)=item.path().symlink_metadata() &&
                st.is_file() && st.modified().is_ok_and(|at|at<cutoff){let _=fs::remove_file(item.path());}
        }
    }
    let partial=root.join(format!("{name}.jsonl"));
    let mut entries=Vec::new();
    let mut journal=None;
    for entry in rx {
        // Even if a partial write fails, all observed entries are still in the
        // final document if storage is available again when the call ends.
        let added=(||->io::Result<()>{
            if journal.is_none(){
                journal=Some(OpenOptions::new().create(true).append(true).mode(0o600)
                    .custom_flags(nix::libc::O_NOFOLLOW|nix::libc::O_NONBLOCK).open(&partial)?);
            }
            if let Some(file)=journal.as_mut(){writeln!(file,"{entry}")?;}
            Ok(())
        })();
        if let Err(err)=added{eprintln!("clarp-host: live call transcript append: {}",err.kind());journal=None;}
        entries.push(entry);
    }
    drop(journal);
    let doc=transcript::document(meta,started,&ended(),&entries);
    let temp=root.join(format!("{name}.{}.tmp",crate::ids::hex(8)));
    let mut output=OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp)?;
    let result=(||->io::Result<()>{
        serde_json::to_writer_pretty(&mut output,&doc)?;
        output.sync_all()?;
        fs::rename(&temp,root.join(format!("{name}.json")))?;
        directory.sync_all()?;
        if !entries.is_empty(){fs::remove_file(partial)?;}
        Ok(())
    })();
    if result.is_err(){let _=fs::remove_file(temp);}
    result
}
