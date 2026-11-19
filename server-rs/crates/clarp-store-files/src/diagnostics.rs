use clarp_ports::diagnostics::DiagnosticStore;
use std::fs::{self, DirBuilder, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;
const HOUR: i64 = 3_600_000;
pub struct HourFiles {
    directory: PathBuf,
    cap: u64,
}
impl HourFiles {
    pub fn new(directory: PathBuf, cap: u64) -> Self {
        Self { directory, cap }
    }
}
impl DiagnosticStore for HourFiles {
    fn append(&mut self, now: i64, records: &[(i64, Vec<u8>)]) -> io::Result<usize> {
        let hour = now.div_euclid(HOUR);
        let mut used = 0u64;
        match fs::symlink_metadata(&self.directory) {
            Ok(m) if !m.is_dir() => {
                return Err(io::Error::other("diagnostic directory unavailable"));
            }
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                if records.is_empty() {
                    return Ok(0);
                }
                DirBuilder::new().mode(0o700).create(&self.directory)?;
            }
            Err(e) => return Err(e),
        }
        fs::set_permissions(&self.directory, fs::Permissions::from_mode(0o700))?;
        for item in fs::read_dir(&self.directory)? {
            let item = item?;
            let path = item.path();
            if !item.file_type()?.is_file() {
                return Err(io::Error::other("unexpected diagnostic entry"));
            }
            let bucket = path
                .file_stem()
                .and_then(|v| v.to_str())
                .and_then(|v| v.parse::<i64>().ok());
            if bucket.is_some_and(|h| h <= hour - 24 || h > hour) {
                fs::remove_file(path)?;
            } else {
                used = used.saturating_add(item.metadata()?.len());
            }
        }
        let mut dropped = 0;
        let mut files = std::collections::BTreeMap::new();
        for (at, record) in records {
            let h = at.div_euclid(HOUR);
            let size = record.len() as u64 + 1;
            if h <= hour - 24 || h > hour || used.saturating_add(size) > self.cap {
                dropped += 1;
                continue;
            }
            let file = match files.entry(h) {
                std::collections::btree_map::Entry::Occupied(e) => e.into_mut(),
                std::collections::btree_map::Entry::Vacant(e) => e.insert(
                    OpenOptions::new()
                        .create(true)
                        .append(true)
                        .mode(0o600)
                        .open(self.directory.join(format!("{h}.jsonl")))?,
                ),
            };
            file.write_all(record)?;
            file.write_all(b"\n")?;
            used += size;
        }
        Ok(dropped)
    }
}

impl clarp_ports::diagnostics::DiagnosticUsage for HourFiles {
    fn usage(&self) -> io::Result<(usize, u64)> {
        let items = match fs::read_dir(&self.directory) {
            Ok(items) => items,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok((0, 0)),
            Err(e) => return Err(e),
        };
        let mut n = 0;
        let mut bytes = 0;
        for item in items {
            let item = item?;
            if item.file_type()?.is_file() {
                n += 1;
                bytes += item.metadata()?.len();
            }
        }
        Ok((n, bytes))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_ports::clock::{Clock, ManualClock};
    #[test]
    fn diagnostic_files_expire_while_disabled_and_refuse_the_newest_at_capacity() {
        let path = std::env::temp_dir().join(format!("clarp-diag-cap-{}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        let clock = ManualClock::starting_at(0);
        let mut store = HourFiles::new(path.clone(), 10);
        assert_eq!(store.append(clock.now_ms(), &[]).unwrap(), 0);
        assert!(!path.exists());
        assert_eq!(store.append(0,&[(0,b"1234".to_vec())]).unwrap(),0);
        let records = vec![(0, b"5678".to_vec()); 99];
        assert_eq!(store.append(0, &records).unwrap(), 98);
        assert_eq!(fs::read(path.join("0.jsonl")).unwrap(), b"1234\n5678\n");
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(path.join("0.jsonl"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        clock.advance_ms(24 * HOUR - 1);
        assert_eq!(
            store
                .append(clock.now_ms(), &[(clock.now_ms(), vec![])])
                .unwrap(),
            1
        );
        clock.advance_ms(1);
        let mut reopened = HourFiles::new(path.clone(), 10);
        reopened.append(clock.now_ms(), &[]).unwrap();
        assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
        assert_eq!(
            reopened
                .append(clock.now_ms(), &[(0, vec![1]), (clock.now_ms(), vec![7])])
                .unwrap(),
            1
        );
        fs::remove_dir_all(path).unwrap();
    }
}
