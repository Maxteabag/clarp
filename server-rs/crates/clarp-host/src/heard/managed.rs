//! The id alone names the removable entry. A registry path is diagnostic data.
use crate::files::{beneath, open};
use std::fs::File;
use std::path::{Component, Path, PathBuf};

pub(super) struct ManagedFolder {
    parent: File,
    name: String,
    directory: bool,
}

pub(super) fn refused(id: &str, reason: &str) -> String {
    eprintln!("clarp-host: transcription model refused: model={id} reason={reason}");
    format!("refusing to remove {id}: its folder is a link or outside the managed models folder")
}

impl ManagedFolder {
    pub(super) fn checked(root: &Path, id: &str) -> Result<Option<Self>, String> {
        let refused = |reason| refused(id, reason);
        let name = id.replace(':', "--");
        if Path::new(&name).file_name() != Some(name.as_ref()) {
            return Err(refused("outside"));
        }
        let absolute_root = std::path::absolute(root).map_err(|_| refused("outside"))?;
        let mut parent = open(Path::new("/"), true).map_err(|_| refused("outside"))?;
        for component in absolute_root.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(part) => {
                    let child = beneath(&parent, part.to_str().ok_or_else(|| refused("outside"))?);
                    parent = match open(&child, true) {
                        Ok(dir) => dir,
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                        Err(_) => return Err(refused("outside")),
                    };
                }
                _ => return Err(refused("outside")),
            }
        }
        let kind = match std::fs::symlink_metadata(beneath(&parent, &name)) {
            Ok(meta) => meta.file_type(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.to_string()),
        };
        if kind.is_symlink() {
            return Err(refused("link"));
        }
        Ok(Some(Self {
            parent,
            name,
            directory: kind.is_dir(),
        }))
    }

    pub(super) fn note_stored(root: &Path, id: &str, stored: &serde_json::Value) {
        let expected = std::path::absolute(root)
            .ok()
            .map(|dir| dir.join(id.replace(':', "--")));
        if !stored
            .as_str()
            .filter(|s| !s.is_empty())
            .is_some_and(|s| Some(Path::new(s)) == expected.as_deref())
        {
            eprintln!("clarp-host: transcription model removal: model={id} reason=stored_path");
        }
    }

    fn path(&self) -> PathBuf {
        beneath(&self.parent, &self.name)
    }

    pub(super) fn remove(self) -> std::io::Result<()> {
        if self.directory {
            std::fs::remove_dir_all(self.path())
        } else {
            std::fs::remove_file(self.path())
        }
    }
}
