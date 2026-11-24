//! The digest of what a lane is started with: a lane whose digest differs from the one wanted
//! is retired while idle and started again ([`super::Endpoint::place`]). A driver puts in it
//! everything its tool reads only when it starts: its options, and the files a kept process
//! would not read again (a program, its configuration, an MCP set the Host does not parse).

use std::io::{self, Read};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use sha2::{Digest, Sha256};

pub(crate) struct OptionsDigest(Sha256);

impl OptionsDigest {
    pub(crate) fn new() -> Self {
        Self(Sha256::new())
    }

    /// Words as they are, run on from what came before: the digest of several is that of their
    /// concatenation. Codex's options and home are digested so, which keeps the digest a running
    /// Codex lane was started with.
    pub(crate) fn text(mut self, text: &str) -> Self {
        self.0.update(text.as_bytes());
        self
    }

    /// A file's whole content, under its path, so a change of content is a change and a touch
    /// is not. A file that is not there is said as missing. One over `most` bytes is an error:
    /// its driver keeps no lane rather than digest less than it reads.
    #[allow(
        dead_code,
        reason = "the seam for the OpenCode and Muse drivers (issue 761); held by its tests"
    )]
    pub(crate) fn file(mut self, path: &Path, most: u64) -> io::Result<Self> {
        self.frame(b"file", path.as_os_str().as_encoded_bytes());
        match std::fs::File::open(path) {
            Ok(file) => {
                let mut content = Vec::new();
                file.take(most + 1).read_to_end(&mut content)?;
                if content.len() as u64 > most {
                    return Err(io::Error::other(format!(
                        "{} is larger than {most} bytes",
                        path.display()
                    )));
                }
                self.frame(b"content", &content);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => self.frame(b"missing", b""),
            Err(error) => return Err(error),
        }
        Ok(self)
    }

    /// A file's path, length and time of change, not its content: for one too large to read at
    /// every turn (a program), or that a tool reads only by its time (Muse's settings).
    #[allow(
        dead_code,
        reason = "the seam for the OpenCode and Muse drivers (issue 761); held by its tests"
    )]
    pub(crate) fn stamp(mut self, path: &Path) -> Self {
        self.frame(b"stamp", path.as_os_str().as_encoded_bytes());
        let seen = std::fs::metadata(path).map_or_else(
            |_| "missing".to_owned(),
            |meta| format!("{} {} {}", meta.len(), meta.mtime(), meta.mtime_nsec()),
        );
        self.frame(b"seen", seen.as_bytes());
        self
    }

    pub(crate) fn finish(self) -> String {
        format!("{:x}", self.0.finalize())
    }

    /// A part said with its kind and length, so no two lists of parts digest alike.
    fn frame(&mut self, kind: &[u8], bytes: &[u8]) {
        self.0.update(kind);
        self.0.update((bytes.len() as u64).to_le_bytes());
        self.0.update(bytes);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::OptionsDigest;
    use sha2::{Digest, Sha256};

    #[test]
    fn the_codex_digest_is_what_it_was() {
        // As `lane::codex` digested its options before the layer: a running lane is kept.
        let resume = serde_json::json!({"cwd": "/w", "model": "m"});
        let home = "/h";
        let before = format!(
            "{:x}",
            Sha256::digest(format!("{resume}{home}").as_bytes())
        );
        let now = OptionsDigest::new()
            .text(&resume.to_string())
            .text(home)
            .finish();
        assert_eq!(now, before);
    }

    #[test]
    fn a_files_content_moves_the_digest_and_a_touch_moves_only_its_stamp() {
        let dir = std::env::temp_dir().join(format!("lane-digest-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.json");
        let content = |path: &std::path::Path| {
            OptionsDigest::new()
                .file(path, 1024)
                .unwrap()
                .finish()
        };
        let stamp = |path: &std::path::Path| OptionsDigest::new().stamp(path).finish();

        let missing = content(&path);
        std::fs::write(&path, "{\"a\":1}").unwrap();
        let first = content(&path);
        let first_stamp = stamp(&path);
        assert_ne!(missing, first);

        // The same content written again a second later: content alike, stamp not.
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(5);
        std::fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(later)
            .unwrap();
        assert_eq!(content(&path), first);
        assert_ne!(stamp(&path), first_stamp);

        std::fs::write(&path, "{\"a\":2}").unwrap();
        assert_ne!(content(&path), first);

        // Too large to digest whole: refused, never digested in part.
        std::fs::write(&path, vec![b'x'; 2048]).unwrap();
        assert!(OptionsDigest::new().file(&path, 1024).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
