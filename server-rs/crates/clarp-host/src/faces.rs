//! The faces agents are shown with: the pictures this Host ships, and where an app fetches
//! them.
//!
//! Which picture an agent has follows from its name, its tool and its model
//! ([`clarp_kernel::portraits`]). Here is what needs a disk: whether a picture is there,
//! what is in it, and handing it out. A picture's address carries its content, so an app
//! that fetched it keeps it until it is another picture; asked for under that address it
//! may be kept for a year, asked for without it is checked each time.
//!
//! The pictures are files beside the Host (`--static`, the directory that holds `avatars/`).
//! A Host that was given none shows its agents without a face.

use std::collections::HashMap;
use std::path::{Path as FilePath, PathBuf};
use std::sync::{Mutex, PoisonError};
use std::time::SystemTime;

use clarp_kernel::portraits;
use sha2::{Digest, Sha256};

pub struct Faces {
    /// The directory that holds `avatars/`. `None`: this Host ships no pictures.
    root: Option<PathBuf>,
    /// What is in each file, for as long as the file is the same: when it was written, how
    /// long it is, and sixteen signs of its SHA-256.
    versions: Mutex<HashMap<PathBuf, (SystemTime, u64, String)>>,
}

impl Faces {
    pub fn new(root: Option<PathBuf>) -> Self {
        Self {
            root,
            versions: Mutex::new(HashMap::new()),
        }
    }

    /// What is in a file, read once for every time the file changes. `None`: no file that
    /// can be read.
    fn version(&self, path: &FilePath) -> Option<String> {
        let found = std::fs::metadata(path)
            .ok()
            .filter(std::fs::Metadata::is_file)?;
        let written = found.modified().ok()?;
        let key = (written, found.len());
        {
            let versions = self.versions.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some((at, length, version)) = versions.get(path)
                && (*at, *length) == key
            {
                return Some(version.clone());
            }
        }
        let bytes = std::fs::read(path).ok()?;
        let digest = Sha256::digest(&bytes);
        let version: String = digest
            .iter()
            .take(8)
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let mut versions = self.versions.lock().unwrap_or_else(PoisonError::into_inner);
        versions.insert(path.to_path_buf(), (key.0, key.1, version.clone()));
        Some(version)
    }

    /// The first of these files that is there, with what is in it.
    fn first(&self, under: &str, files: Vec<String>) -> Option<(String, String)> {
        let root = self.root.as_ref()?.join(under);
        files.into_iter().find_map(|file| {
            let version = self.version(&root.join(&file))?;
            Some((file, version))
        })
    }

    /// Where the picture of an agent that chose none is fetched: the one of its name, else
    /// the one of its tool. Empty: there is neither.
    pub fn of(&self, persona: &str, backend: &str) -> String {
        match self.first("avatars", portraits::files(persona, backend)) {
            Some((file, version)) => portraits::address(portraits::AVATARS, &file, &version),
            None => String::new(),
        }
    }

    /// Where the picture a janitor is shown with is fetched: the one of its job, else the
    /// one every janitor has. Empty: this Host ships neither.
    pub fn janitor(&self, file: &str) -> String {
        let files = vec![file.to_string(), "janitor-default.jpg".to_string()];
        match self.first("janitor-avatars", files) {
            Some((file, version)) => format!("/static/janitor-avatars/{file}?v={version}"),
            None => String::new(),
        }
    }

    /// Where the picture of an agent on the model it runs is fetched, for a name that has
    /// one drawn for that family of model. Empty: it has none.
    pub fn on_model(
        &self,
        persona: &str,
        backend: &str,
        model: &str,
        default_model: &str,
    ) -> String {
        let files = portraits::model_files(persona, backend, model, default_model);
        match self.first("avatars/models", files) {
            Some((file, version)) => format!("{}/{file}?v={version}", portraits::MODEL_AVATARS),
            None => String::new(),
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn an_agent_has_the_picture_of_its_name_else_of_its_tool_and_none_where_none_is_shipped() {
        let root = std::env::temp_dir().join(format!("clarp-faces-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("avatars/models")).unwrap();
        std::fs::write(root.join("avatars/claire.png"), b"claire").unwrap();
        std::fs::write(root.join("avatars/default-codex.png"), b"codex").unwrap();
        std::fs::write(
            root.join("avatars/models/claire.opus.png"),
            b"claire on opus",
        )
        .unwrap();
        let faces = Faces::new(Some(root.clone()));
        // sha256("claire") begins 0bb5...: what is in the file is in its address.
        let claire = faces.of("Claire", "claude");
        assert!(
            claire.starts_with("/static/avatars/claire.png?v="),
            "{claire}"
        );
        assert_eq!(claire.len(), "/static/avatars/claire.png?v=".len() + 16);
        assert_eq!(
            faces.of("Claire", "claude"),
            claire,
            "read once, said the same"
        );
        assert!(
            faces
                .of("Nobody", "codex")
                .starts_with("/static/avatars/default-codex.png?v=")
        );
        assert_eq!(
            faces.of("Nobody", "claude"),
            "",
            "no picture of the name, none of the tool"
        );
        assert!(
            faces
                .on_model("Claire", "claude", "claude-opus-5-5", "")
                .starts_with("/static/avatars/models/claire.opus.png?v=")
        );
        assert_eq!(
            faces.on_model("Claire", "claude", "claude-sonnet-5-5", ""),
            ""
        );
        assert_eq!(
            faces.on_model("Claire", "claude", "", ""),
            "",
            "Claude Code names no family"
        );
        // Another picture under the same name has another address.
        std::fs::write(root.join("avatars/claire.png"), b"claire, drawn again").unwrap();
        assert_ne!(faces.of("Claire", "claude"), claire);
        // A Host that ships none shows no face.
        let none = Faces::new(None);
        assert_eq!(
            (
                none.of("Claire", "claude"),
                none.on_model("Claire", "claude", "opus", "")
            ),
            (String::new(), String::new())
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
