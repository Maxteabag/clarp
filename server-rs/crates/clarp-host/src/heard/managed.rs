//! Removal is confined to the single managed directory derived from the selected model id.

use crate::files::{beneath, open};
use std::fs::File;
use std::path::{Component, Path, PathBuf};

pub(super) struct ManagedFolder {
    parent: File,
    name: String,
}

impl ManagedFolder {
    pub(super) fn checked(
        root: &Path,
        id: &str,
        target: &Path,
        resuming: bool,
    ) -> Result<Option<Self>, String> {
        let unsafe_target =
            || format!("refusing to remove anything but the managed folder for {id}");
        let name = id.replace(':', "--");
        let resolved = target.canonicalize().unwrap_or_else(|_| target.to_owned());
        let absolute_root = std::path::absolute(root).map_err(|e| e.to_string())?;
        let resolved_root = root
            .canonicalize()
            .unwrap_or_else(|_| absolute_root.clone());
        if !resolved.starts_with(&resolved_root) {
            return Err(format!(
                "refusing to remove model outside managed caches: {}",
                resolved.display()
            ));
        }
        if Path::new(&name).file_name() != Some(name.as_ref())
            || target != absolute_root.join(&name)
            || resolved != target
        {
            return Err(unsafe_target());
        }
        // Pin each component. Replacing a parent with a link after validation cannot
        // redirect the later directory operation into another tree.
        let mut parent = open(Path::new("/"), true).map_err(|e| e.to_string())?;
        for component in absolute_root.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(part) => {
                    let child = beneath(&parent, part.to_str().ok_or_else(unsafe_target)?);
                    parent = match open(&child, true) {
                        Ok(dir) => dir,
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound && resuming => {
                            return Ok(None);
                        }
                        Err(_) => return Err(unsafe_target()),
                    };
                }
                _ => return Err(unsafe_target()),
            }
        }
        let folder = Self { parent, name };
        match open(&folder.path(), true) {
            Ok(_) => Ok(Some(folder)),
            Err(error) if resuming && error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(unsafe_target()),
        }
    }

    fn path(&self) -> PathBuf {
        beneath(&self.parent, &self.name)
    }

    pub(super) fn remove(self) -> std::io::Result<()> {
        std::fs::remove_dir_all(self.path())
    }
}
