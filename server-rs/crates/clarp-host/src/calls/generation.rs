//! A listen-first opening belongs to a kernel boot and a Host instance. An
//! ordinary process restart preserves both. Read failures deny the opening;
//! reading must never mint a replacement for an absent or damaged token.
use nix::libc::{O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK};
use serde_json::{Value, json};
use std::fs::{self, DirBuilder, File, OpenOptions, Permissions};
use std::io::{self, ErrorKind, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

fn invalid() -> io::Error {
    io::Error::new(ErrorKind::InvalidData, "unprovable host generation")
}

fn uuid(file: File) -> io::Result<String> {
    let mut bytes = Vec::new();
    file.take(65).read_to_end(&mut bytes)?;
    let text = std::str::from_utf8(&bytes).map_err(|_| invalid())?;
    let text = text.strip_suffix('\n').unwrap_or(text);
    if text.len() != 36
        || !text.bytes().enumerate().all(|(i, c)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
    {
        return Err(invalid());
    }
    Ok(text.into())
}

fn dir(root: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW | O_DIRECTORY)
        .open(root.join("backups"))?;
    let meta = file.metadata()?;
    if meta.uid() != nix::unistd::geteuid().as_raw() || meta.mode() & 0o022 != 0 {
        return Err(invalid());
    }
    Ok(file)
}

fn under(dir: &File) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()))
}
fn token(dir: &File) -> io::Result<String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW | O_NONBLOCK)
        .open(under(dir).join("instance.generation"))?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.uid() != nix::unistd::geteuid().as_raw()
        || meta.mode() & 0o7777 != 0o600
    {
        return Err(invalid());
    }
    uuid(file)
}

pub fn read(root: &Path) -> Value {
    let boot = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW | O_NONBLOCK)
        .open("/proc/sys/kernel/random/boot_id")
        .and_then(uuid);
    match (boot, dir(root).and_then(|dir| token(&dir))) {
        (Ok(boot), Ok(instance)) => json!({"boot":boot,"instance":instance}),
        _ => Value::Null,
    }
}

pub fn bootstrap(root: &Path) -> io::Result<()> {
    match DirBuilder::new().mode(0o700).create(root.join("backups")) {
        Ok(()) => File::open(root)?.sync_all()?,
        Err(e) if e.kind() == ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e),
    }
    let dir = dir(root)?;
    match token(&dir) {
        Ok(_) => return Ok(()),
        Err(e) if e.kind() == ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let root = under(&dir);
    let temp = root.join(format!(".instance-{}", crate::ids::hex(16)));
    let mut file = OpenOptions::new()
        .write(true)
        .mode(0o600)
        .create_new(true)
        .open(&temp)?;
    let result = (|| {
        file.set_permissions(Permissions::from_mode(0o600))?;
        writeln!(file, "{}", crate::ids::uuid())?;
        file.sync_all()?;
        match fs::hard_link(&temp, root.join("instance.generation")) {
            Ok(()) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
        Ok(())
    })();
    fs::remove_file(temp)?;
    result?;
    dir.sync_all()?;
    token(&dir).map(|_| ())
}

pub(super) struct Evidence<'a> {
    pub root: &'a Path,
    pub directory: &'a crate::fleet::Directory,
}

impl clarp_kernel::rings::opening::Proof for Evidence<'_> {
    fn generation(&self) -> Value {
        read(self.root)
    }
    fn stop(&self, id: &str) -> (u64, bool) {
        self.directory.named(id).map_or((0, false), |member| {
            let view = member.agent.view();
            (view.status.stop_generation, view.status.paused)
        })
    }
    fn auth(&self, principal: &str) -> Value {
        self.directory
            .devices
            .values()
            .find(|d| d.device_id == principal)
            .map_or(Value::Null, |d| json!({"token_hash":d.token_hash}))
    }
}
