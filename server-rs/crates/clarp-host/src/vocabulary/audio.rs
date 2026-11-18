//! An opted-in diagnostic clip and its metadata are published by one rename.
//! Neither a short write nor a concurrent lookup can expose half of a clip.
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use crate::edge::{ApiError, App};
use axum::{
    body::Body,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::Response,
};
use serde_json::{Value, json};
const AGE: Duration = Duration::from_secs(14 * 86400);
const COUNT: usize = 500;

fn valid(trace: &str) -> bool {
    (8..=64).contains(&trace.len())
        && trace
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn enabled(dir: &crate::fleet::Directory) -> bool {
    dir.setting("transcription.retain_audio") == Some("true")
}
pub fn exists(root: &Path, trace: &str) -> bool {
    valid(trace)
        && root
            .join("heard")
            .join(format!("{trace}.clip"))
            .symlink_metadata()
            .is_ok_and(|m| m.is_file())
}

pub struct Clip<'a> {
    pub trace:&'a str, pub data:&'a [u8], pub kind:&'a str, pub session:&'a str,
    pub run:i64, pub model:&'a str,
}

pub fn retain(root:&Path,clip:Clip<'_>,now:i64)->io::Result<()> {
    let Clip{trace,data,kind,session,run,model}=clip;
    if !valid(trace) || data.is_empty() {
        return Ok(());
    }
    let dir = root.join("heard");
    fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(&dir)?;
    let target = dir.join(format!("{trace}.clip"));
    let temp = dir.join(format!(".{}.tmp", crate::ids::hex(12)));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    let meta=json!({"trace_id":trace,"content_type":kind,"session":session,"run_id":run,"model":model,"bytes":data.len(),"created_at":now}).to_string();
    let write = (|| {
        file.write_all(&(meta.len() as u32).to_le_bytes())?;
        file.write_all(meta.as_bytes())?;
        file.write_all(data)?;
        file.sync_all()?;
        fs::rename(&temp, target)
    })();
    if write.is_err() {
        let _ = fs::remove_file(temp);
    }
    write?;
    sweep(&dir, SystemTime::now())
}

fn sweep(root: &Path, now: SystemTime) -> io::Result<()> {
    let mut files: Vec<(SystemTime, PathBuf)> = Vec::new();
    for e in root.read_dir()?.flatten() {
        let path = e.path();
        if path.extension().is_some_and(|ext| ext == "clip")
            && let Ok(time) = e.metadata().and_then(|m| m.modified())
        {
            files.push((time, path));
        }
    }
    files.sort_unstable_by(|a, b| b.cmp(a));
    for (i, (when, path)) in files.into_iter().enumerate() {
        if i >= COUNT || now.duration_since(when).unwrap_or_default() > AGE {
            let _ = fs::remove_file(path);
        }
    }
    Ok(())
}

fn load(root: &Path, trace: &str) -> Option<(Value, Vec<u8>)> {
    if !valid(trace) {
        return None;
    }
    let path = root.join("heard").join(format!("{trace}.clip"));
    let mut file = File::options()
        .read(true)
        .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK)
        .open(path)
        .ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut len = [0; 4];
    file.read_exact(&mut len).ok()?;
    let count = u32::from_le_bytes(len) as usize;
    if count > 128 * 1024 {
        return None;
    }
    let mut meta = vec![0; count];
    file.read_exact(&mut meta).ok()?;
    let meta: Value = serde_json::from_slice(&meta).ok()?;
    let size = meta["bytes"].as_u64()?;
    let mut bytes = Vec::new();
    if file
        .take(size.saturating_add(1))
        .read_to_end(&mut bytes)
        .ok()? as u64
        != size
    {
        return None;
    }
    Some((meta, bytes))
}

pub async fn get(
    State(app): State<App>,
    Query(q): Query<std::collections::HashMap<String, String>>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    let root = app.config.data_dir.clone();
    let trace = q.get("trace_id").cloned().unwrap_or_default();
    let (meta, bytes) = tokio::task::spawn_blocking(move || load(&root, &trace))
        .await
        .ok()
        .flatten()
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "no retained audio for that trace"))?;
    let kind = meta["content_type"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("application/octet-stream");
    let total = bytes.len() as u64;
    let mut response = Response::builder().header("accept-ranges", "bytes");
    let part =
        clarp_kernel::media::byte_range(&crate::media::header_text(&headers, "range"), total);
    let bytes = axum::body::Bytes::from(bytes);
    let body = match part {
        clarp_kernel::media::Part::Whole => bytes,
        clarp_kernel::media::Part::Bytes { first, last } => {
            response = response
                .status(StatusCode::PARTIAL_CONTENT)
                .header("content-range", format!("bytes {first}-{last}/{total}"));
            bytes.slice(first as usize..=last as usize)
        }
        clarp_kernel::media::Part::Missing => {
            response = response
                .status(StatusCode::RANGE_NOT_SATISFIABLE)
                .header("content-range", format!("bytes */{total}"));
            axum::body::Bytes::new()
        }
    };
    response
        .header("content-type", kind)
        .header("content-length", body.len())
        .body(Body::from(body))
        .map_err(|_| {
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "retained audio unavailable",
            )
        })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[test]
    fn retained_audio_obeys_trace_age_and_count_limits(){
        let fixture:Value=serde_json::from_str(include_str!("../../../clarp-kernel/tests/fixtures/vocabulary-packs-vectors.json")).unwrap();
        for case in fixture["traces"].as_array().unwrap(){
            assert_eq!(case["valid"],valid(case["trace"].as_str().unwrap()));
        }
        let root=std::env::temp_dir().join(format!("clarp-vocab-retention-{}",crate::ids::hex(8)));
        fs::create_dir(&root).unwrap();
        let now=SystemTime::UNIX_EPOCH+Duration::from_secs(2_000_000_000);
        let make=|id:usize,moment:SystemTime| {
            let path=root.join(format!("{id:08x}.clip"));
            File::create(&path).unwrap().set_modified(moment).unwrap();path
        };
        let expiring=make(0,now-AGE);
        let expired=make(1,now-AGE-Duration::from_secs(1));
        let unrelated=root.join("unrelated");
        fs::write(&unrelated,b"fixture").unwrap();
        sweep(&root,now).unwrap();
        assert!(expiring.exists());
        assert!(!expired.exists());
        for i in 1..=500 {make(i,now+Duration::from_secs(i as u64));}
        sweep(&root,now).unwrap();
        assert!(!expiring.exists());assert!(unrelated.exists());
        assert_eq!(fs::read_dir(&root).unwrap().count(),501);
        fs::remove_dir_all(root).unwrap();
    }
}
