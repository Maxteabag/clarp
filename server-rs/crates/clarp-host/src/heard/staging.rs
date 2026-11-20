//! Staging and publication are operations on an open directory. Only catalog ids
//! are accepted, so request paths and stored paths never select a removal target.
use crate::files::{beneath, open};
use clarp_kernel::heard::inventory;
use nix::fcntl::{RenameFlags, renameat2};
use nix::sys::statvfs::fstatvfs;
use std::fs::{DirBuilder, File};
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Component, Path, PathBuf};

pub(super) struct Folders {
    root: File,
    path: PathBuf,
    stage: String,
    model: String,
}

fn invalid(why: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, why)
}

fn open_child(parent: &File, name: &str, create: bool) -> io::Result<File> {
    let path = beneath(parent, name);
    if create {
        match DirBuilder::new().mode(0o700).create(&path) {
            Ok(()) => parent.sync_all()?,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e),
        }
    }
    open(&path, true)
}

impl Folders {
    pub(super) fn open(root: &Path, id: &str, create: bool) -> io::Result<Self> {
        inventory::install_model(id).map_err(io::Error::other)?;
        let path = std::path::absolute(root)?;
        let mut parent = open(Path::new("/"), true)?;
        for part in path.components() {
            match part {
                Component::RootDir => (),
                Component::Normal(name) => {
                    let name = name
                        .to_str()
                        .ok_or_else(|| invalid("invalid models directory"))?;
                    parent = open_child(&parent, name, create)?;
                }
                _ => return Err(invalid("unsafe models directory")),
            }
        }
        let model = id.replace(':', "--");
        Ok(Self {
            root: parent,
            path,
            stage: format!(".install-{model}"),
            model,
        })
    }

    pub(super) fn staging(&self, create: bool) -> io::Result<File> {
        open_child(&self.root, &self.stage, create)
    }

    pub(super) fn free_bytes(&self) -> io::Result<u64> {
        let stats = fstatvfs(&self.root)?;
        Ok(stats
            .blocks_available()
            .saturating_mul(stats.fragment_size()))
    }

    pub(super) fn path(&self) -> PathBuf {
        self.path.join(&self.model)
    }

    fn rename(&self, from: &str, to: &str) -> io::Result<()> {
        let fd = Some(self.root.as_raw_fd());
        renameat2(fd, from, fd, to, RenameFlags::RENAME_NOREPLACE)?;
        self.root.sync_all()
    }

    pub(super) fn publish(&self, stage: &File, maximum: u64) -> io::Result<()> {
        let at = self.staging(false)?.metadata()?;
        let held = stage.metadata()?;
        if (at.dev(), at.ino()) != (held.dev(), held.ino()) {
            return Err(invalid("staging directory was replaced"));
        }
        if staged_bytes(stage)? > maximum {
            return Err(invalid("model exceeds catalog byte limit"));
        }
        for name in ["config.json", "model.bin", "tokenizer.json"] {
            let file = open(&beneath(stage, name), false)?;
            if !file.metadata()?.is_file() {
                return Err(invalid("incomplete installed model"));
            }
        }
        self.rename(&self.stage, &self.model)
    }

    pub(super) fn undo_publish(&self, device: u64, inode: u64) -> io::Result<()> {
        let same = |file: &File| {
            file.metadata()
                .map(|m| (m.dev(), m.ino()) == (device, inode))
        };
        match self.staging(false) {
            Ok(stage) if same(&stage)? => return Ok(()),
            Ok(_) => return Err(invalid("staging directory belongs to another install")),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e),
        }
        let published = open_child(&self.root, &self.model, false)?;
        if !same(&published)? {
            return Err(invalid("published directory belongs to another install"));
        }
        self.rename(&self.model, &self.stage)
    }

    pub(super) fn remove_stage(&self) -> io::Result<()> {
        match self.staging(false) {
            Ok(_) => {
                std::fs::remove_dir_all(beneath(&self.root, &self.stage))?;
                self.root.sync_all()
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e),
        }
    }
}

pub(super) fn staged_bytes(directory: &File) -> io::Result<u64> {
    let mut bytes = 0u64;
    for child in std::fs::read_dir(beneath(directory, ""))? {
        let child = child?;
        let name = child.file_name();
        let name = name
            .to_str()
            .ok_or_else(|| invalid("invalid staging filename"))?;
        let kind = child.file_type()?;
        let size = if kind.is_dir() {
            staged_bytes(&open_child(directory, name, false)?)?
        } else if kind.is_file() {
            let info = open(&beneath(directory, name), false)?.metadata()?;
            if !info.is_file() || info.nlink() != 1 {
                return Err(invalid("unsafe staged file"));
            }
            info.len()
        } else {
            return Err(invalid("unsafe entry in staging"));
        };
        bytes = bytes
            .checked_add(size)
            .ok_or_else(|| invalid("staging size overflow"))?;
    }
    Ok(bytes)
}
