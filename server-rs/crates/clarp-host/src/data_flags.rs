//! The flags the Host's data folders carry, so that what SQLite writes there is written in
//! place (issue 848).
//!
//! On a btrfs filesystem mounted with compression, a database page written into a compressed,
//! copy-on-write file rewrites a whole compressed extent, and its fsync commits that and its
//! metadata. On 26 Nov 2026 a laptop's Host stalled for minutes with single commits of 15 to 48
//! s; its search index was 192 MB in 21 418 extents, the fleet's file 22 MB in 4 659.
//!
//! So at its start the Host sets no-compression (`FS_NOCOMP_FL`, `chattr +m`) on its data
//! folder, the agents' folder and every agent's folder: a file made in them afterwards inherits
//! it. The agents' logs keep copy-on-write and btrfs's checksums. The search index, which the
//! Host can always make again, lives in a folder of its own that is also no-copy-on-write
//! (`FS_NOCOW_FL`, `chattr +C`).
//!
//! Only folders are touched, never a file, and never a file SQLite has open: a flag on a folder
//! changes nothing of what is in it already. Existing files keep their extents: moving them is
//! the owner's decision, with the Host stopped. A filesystem that does not take a flag (ext4,
//! tmpfs, xfs) is passed over without a word.

use std::path::{Path, PathBuf};

use rustix::fs::{IFlags, Mode, OFlags};
use rustix::io::Errno;

/// `FS_NOCOMP_FL`: no compression for what is written from now on.
const NO_COMPRESSION: u32 = 0x0000_0400;

/// The folder the search index lives in, in the data folder.
pub const SEARCH_FOLDER: &str = "search";

/// What became of a flag on one folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Marked {
    /// It carries the flag now.
    Set,
    /// It carried it already.
    Already,
    /// Its filesystem does not keep such a flag.
    Unsupported,
}

/// No compression for what is written in this folder from now on.
pub fn no_compression() -> IFlags {
    IFlags::from_bits_retain(NO_COMPRESSION)
}

/// Sets `flag` on the folder `dir`, and reads it back: a filesystem that answers yes and keeps
/// nothing does not take it.
pub fn mark(dir: &Path, flag: IFlags) -> Result<Marked, Errno> {
    let folder = rustix::fs::open(
        dir,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )?;
    let has = match rustix::fs::ioctl_getflags(&folder) {
        Ok(has) => has,
        Err(why) if not_here(why) => return Ok(Marked::Unsupported),
        Err(why) => return Err(why),
    };
    if has.contains(flag) {
        return Ok(Marked::Already);
    }
    match rustix::fs::ioctl_setflags(&folder, has | flag) {
        Ok(()) => {}
        Err(why) if not_here(why) => return Ok(Marked::Unsupported),
        Err(why) => return Err(why),
    }
    let kept = rustix::fs::ioctl_getflags(&folder).is_ok_and(|now| now.contains(flag));
    Ok(if kept { Marked::Set } else { Marked::Unsupported })
}

/// What a filesystem without such flags answers.
fn not_here(why: Errno) -> bool {
    why == Errno::OPNOTSUPP || why == Errno::NOTTY || why == Errno::INVAL
}

/// What the start did, for its one line in the Host's log.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Done {
    pub set: usize,
    pub already: usize,
    pub unsupported: usize,
    /// The folders that could not be marked, and why.
    pub failed: Vec<(PathBuf, String)>,
}

impl Done {
    fn count(&mut self, dir: &Path, marked: Result<Marked, Errno>) {
        match marked {
            Ok(Marked::Set) => self.set += 1,
            Ok(Marked::Already) => self.already += 1,
            Ok(Marked::Unsupported) => self.unsupported += 1,
            Err(why) => self.failed.push((dir.to_path_buf(), why.to_string())),
        }
    }
}

/// Marks the Host's data folders at its start, before anything in them is opened: the data
/// folder, the agents' folder and each agent's, and the search index's own folder (made here),
/// which is also no-copy-on-write.
pub fn at_start(data: &Path) -> Done {
    let mut done = Done::default();
    let agents = data.join("agents");
    let search = data.join(SEARCH_FOLDER);
    for dir in [&agents, &search] {
        if let Err(why) = std::fs::create_dir_all(dir) {
            done.failed.push((dir.clone(), why.to_string()));
        }
    }
    // The parents first: a folder made in them from now on is made with the flag.
    for dir in [data, &agents, &search] {
        done.count(dir, mark(dir, no_compression()));
    }
    // Copy-on-write is turned off while the folder holds no index yet: a file made in it takes
    // it from the folder, and a file made before keeps what it has.
    done.count(&search, mark(&search, IFlags::NOCOW));
    if let Ok(each) = std::fs::read_dir(&agents) {
        for entry in each.flatten() {
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                let dir = entry.path();
                done.count(&dir, mark(&dir, no_compression()));
            }
        }
    }
    done
}

/// Says what the start did, where it did anything or failed: nothing on a filesystem that
/// keeps no such flags.
pub fn tell(done: &Done) {
    if done.set > 0 {
        eprintln!(
            "clarp-host: {} data folders now write without compression ({} did already)",
            done.set, done.already
        );
    }
    for (dir, why) in &done.failed {
        eprintln!(
            "clarp-host: the data folder {} keeps its flags: {why}",
            dir.display()
        );
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    struct Scratch(PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn scratch(name: &str) -> Scratch {
        let dir = std::env::temp_dir().join(format!(
            "clarp-data-flags-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        Scratch(dir)
    }

    fn carries(dir: &Path, flag: IFlags) -> bool {
        let folder = rustix::fs::open(
            dir,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .unwrap();
        rustix::fs::ioctl_getflags(&folder).is_ok_and(|has| has.contains(flag))
    }

    #[test]
    fn a_folder_is_marked_where_its_filesystem_takes_it_and_passed_over_quietly_elsewhere() {
        let place = scratch("mark");
        let first = mark(&place.0, no_compression()).unwrap();
        match first {
            // A filesystem with the flag (btrfs): a second look finds it, and a folder made in
            // it afterwards inherits it.
            Marked::Set | Marked::Already => {
                assert_eq!(mark(&place.0, no_compression()).unwrap(), Marked::Already);
                let fresh = place.0.join("an-agent");
                std::fs::create_dir(&fresh).unwrap();
                assert!(carries(&fresh, no_compression()));
            }
            // ext4, tmpfs, xfs: nothing changes and nothing fails.
            Marked::Unsupported => {
                assert!(!carries(&place.0, no_compression()));
                assert_eq!(mark(&place.0, no_compression()).unwrap(), Marked::Unsupported);
            }
        }
    }

    #[test]
    fn the_start_marks_the_data_folders_makes_the_search_folder_and_fails_nowhere() {
        let place = scratch("start");
        let data = place.0.join("data");
        std::fs::create_dir_all(data.join("agents").join("a1")).unwrap();
        // A file in an agent's folder is never opened: only folders are.
        std::fs::write(data.join("agents").join("a1").join("notes"), b"kept").unwrap();
        let done = at_start(&data);
        assert!(done.failed.is_empty(), "{done:?}");
        assert!(data.join(SEARCH_FOLDER).is_dir());
        // The data folder, the agents', the search folder twice (both flags) and the agent's.
        assert_eq!(done.set + done.already + done.unsupported, 5, "{done:?}");
        // Where the filesystem takes the flag, every folder carries it now, and an agent's
        // folder made afterwards carries it too.
        if done.unsupported == 0 {
            for dir in [&data, &data.join("agents"), &data.join("agents").join("a1")] {
                assert!(carries(dir, no_compression()), "{}", dir.display());
            }
            assert!(carries(&data.join(SEARCH_FOLDER), IFlags::NOCOW));
            let fresh = data.join("agents").join("a2");
            std::fs::create_dir(&fresh).unwrap();
            assert!(carries(&fresh, no_compression()));
        }
        // Again: nothing more to do.
        let again = at_start(&data);
        assert_eq!(again.set, 0, "{again:?}");
        assert!(again.failed.is_empty(), "{again:?}");
    }

    #[test]
    fn a_folder_that_is_not_there_is_an_error_not_a_panic() {
        let place = scratch("absent");
        assert!(mark(&place.0.join("absent"), no_compression()).is_err());
    }
}
