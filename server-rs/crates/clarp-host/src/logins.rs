//! Following the logins of the agents' tools.
//!
//! On a machine where someone else puts another account in place when one is out (a Host
//! beside this one that runs the owner's selector), a turn of this Host whose account is out
//! waits (`clarp_kernel::recovery::Accounts::Follow`). It is tried again now and then; and at
//! once when the tool's login has changed, which is what this notices. It reads the file a
//! tool keeps its login in and nothing else, changes nothing, and keeps of the file only a
//! digest, in memory: a login is never written anywhere and never said.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use sha2::{Digest, Sha256};

use crate::fleet::FleetHandle;

/// What stands for a login file as it is now: nothing of what is in it. `None` when the file
/// is not there or cannot be read.
fn digest(path: &Path) -> Option<[u8; 32]> {
    std::fs::read(path)
        .ok()
        .map(|bytes| Sha256::digest(&bytes).into())
}

/// The login files of the tools this Host can run, by the tool's name.
pub fn files(home: &Path) -> BTreeMap<&'static str, PathBuf> {
    ["claude", "codex"]
        .into_iter()
        .filter_map(|name| {
            let file = clarp_backend::by_id(name)?.login_file(home)?;
            Some((name, file))
        })
        .collect()
}

/// The logins as they were last seen.
#[derive(Debug, Default)]
pub struct Seen {
    by_tool: BTreeMap<&'static str, Option<[u8; 32]>>,
}

impl Seen {
    /// Looks at the login files again. Answers the tools whose login is another one than at
    /// the last look. The first look answers none: nothing is known to have changed.
    pub fn look(&mut self, files: &BTreeMap<&'static str, PathBuf>) -> Vec<&'static str> {
        let mut changed = Vec::new();
        for (tool, file) in files {
            let now = digest(file);
            match self.by_tool.insert(*tool, now) {
                // A login that is gone has not become another one: nothing can be tried.
                Some(before) if before != now && now.is_some() => changed.push(*tool),
                _ => {}
            }
        }
        changed
    }
}

/// Looks at the logins every `every`, for as long as the Host runs, and tells the agents of
/// a tool whose login has changed.
pub fn follow(fleet: FleetHandle, home: PathBuf, every: Duration) {
    let files = files(&home);
    let spawned = std::thread::Builder::new()
        .name("logins".to_string())
        .spawn(move || {
            let mut seen = Seen::default();
            loop {
                for tool in seen.look(&files) {
                    for member in &fleet.directory().members {
                        if member.record.backend == tool {
                            member.agent.login_changed();
                        }
                    }
                }
                std::thread::sleep(every);
            }
        });
    if let Err(error) = spawned {
        eprintln!("clarp-host: the logins are not followed: {error}");
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_login_that_became_another_one_is_noticed_once() {
        let home = std::env::temp_dir().join(format!("clarp-logins-{}", std::process::id()));
        let claude = home.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        let files: BTreeMap<&'static str, PathBuf> =
            [("claude", claude.join(".credentials.json"))].into();
        let mut seen = Seen::default();
        // Nothing is there: nothing to tell. Then a login is put there: that is news.
        assert!(seen.look(&files).is_empty());
        std::fs::write(&files["claude"], b"the first account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        assert!(seen.look(&files).is_empty(), "the same login is no news");
        // Another account is put in its place.
        std::fs::write(&files["claude"], b"another account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        assert!(seen.look(&files).is_empty());
        // A login that is taken away is nothing a turn could be tried on.
        std::fs::remove_file(&files["claude"]).unwrap();
        assert!(seen.look(&files).is_empty());
        std::fs::write(&files["claude"], b"another account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_tools_logins_are_where_the_tools_keep_them() {
        let found = files(Path::new("/home/someone"));
        assert_eq!(found.len(), 2);
        assert!(found["claude"].ends_with(".credentials.json"));
        assert!(found["codex"].ends_with("auth.json"));
    }
}
