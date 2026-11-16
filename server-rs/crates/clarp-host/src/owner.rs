//! The owner's own file of settings: a config of the Python Host's kind, read where it is.
//!
//! On a machine that has a Python Host, the owner's keys and his choices of speech are in
//! that Host's config. This Host reads them there, each time it needs one, and keeps
//! nothing: a key the owner changes is the key that is used next, and no secret is copied
//! into this Host's own files. A Host that is told of no such file has what its own
//! environment says, and nothing else.

use std::path::PathBuf;

use toml::{Table, Value};

/// Where the owner's file is, when this Host was told of one.
#[derive(Debug, Clone, Default)]
pub struct OwnerFile {
    path: Option<PathBuf>,
}

/// The text at the end of a path of tables, without the space around it, when there is one
/// and it is not empty.
pub fn text_at(root: &Table, path: &[&str]) -> Option<String> {
    let (last, tables) = path.split_last()?;
    let mut table = root;
    for name in tables {
        table = table.get(*name)?.as_table()?;
    }
    let text = table.get(*last)?.as_str()?.trim();
    (!text.is_empty()).then(|| text.to_string())
}

/// The texts of a table that maps names to texts (`[gemini_tts.voices]`), in the order the
/// owner wrote them. What is no text in it is passed over.
pub fn texts_at(root: &Table, path: &[&str]) -> Vec<(String, String)> {
    let mut table = root;
    for name in path {
        match table.get(*name).and_then(Value::as_table) {
            Some(inner) => table = inner,
            None => return Vec::new(),
        }
    }
    let texts = table.iter().filter_map(|(name, value)| {
        let text = value.as_str()?.trim();
        Some((name.clone(), text.to_string()))
    });
    texts.collect()
}

impl OwnerFile {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self { path }
    }

    /// The file as it is now. `None`: no file was named, it is not there, or what is in it
    /// is no TOML.
    pub fn read(&self) -> Option<Table> {
        let text = std::fs::read_to_string(self.path.as_ref()?).ok()?;
        text.parse::<Table>().ok()
    }

    /// The key of a provider of speech: what the owner's file has for it, else what this
    /// Host's own environment has. Read now, kept nowhere.
    pub fn key(&self, provider: &str) -> Option<String> {
        let (path, variable): (&[&str], &str) = match provider {
            "elevenlabs" => (&["elevenlabs", "api_key"], "ELEVEN_API_KEY"),
            "cartesia" => (&["cartesia", "api_key"], "CARTESIA_API_KEY"),
            "gemini" => (&["gemini_tts", "api_key"], "GEMINI_API_KEY"),
            // Google's other door has a key of its own.
            "vertex" => (&["gemini_tts", "vertex_api_key"], "GOOGLE_VERTEX_API_KEY"),
            _ => return None,
        };
        let written = self.read().and_then(|file| text_at(&file, path));
        let own = || std::env::var(variable).ok();
        written.or_else(|| {
            own()
                .map(|key| key.trim().to_string())
                .filter(|key| !key.is_empty())
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    const FILE: &str = r#"
# The Python Host's own file, as much of it as matters here.
[server]
port = 7682
api_key = "the server's, which is another"

[elevenlabs]  # speech
model = 'eleven_v3'
api_key = "sk_\"quoted\"\\ and \u00e9"   # the key
speed = 1.1

[tts]
provider = " Gemini "
fallback = "cartesia"

[tts.agents.Ingrid]
provider = "gemini"
gemini_voice = "Charon"

[tts.agents."Name With Space"]
cartesia_voice = "ca-1"

[gemini_tts]
vertex_api_key = "vertex-key"
voices = { Maximus = "Charon", "Ünï" = "Aoede", odd = 7 }

[gemini_tts.vertex_voices]
Maximus = "Charon-v"

[cartesia]
api_key = """
several lines are a text too
"""

[whisper]
model = "small.en"
enabled = true
"#;

    #[test]
    fn a_text_is_read_where_it_is_in_the_owners_file() {
        let file: Table = FILE.parse().unwrap();
        assert_eq!(
            text_at(&file, &["elevenlabs", "api_key"]).as_deref(),
            Some("sk_\"quoted\"\\ and \u{e9}")
        );
        assert_eq!(
            text_at(&file, &["elevenlabs", "model"]).as_deref(),
            Some("eleven_v3")
        );
        assert_eq!(
            text_at(&file, &["tts", "provider"]).as_deref(),
            Some("Gemini")
        );
        assert_eq!(
            text_at(&file, &["tts", "agents", "Ingrid", "gemini_voice"]).as_deref(),
            Some("Charon")
        );
        assert_eq!(
            text_at(
                &file,
                &["tts", "agents", "Name With Space", "cartesia_voice"]
            )
            .as_deref(),
            Some("ca-1")
        );
        assert_eq!(
            text_at(&file, &["whisper", "model"]).as_deref(),
            Some("small.en")
        );
        assert_eq!(
            text_at(&file, &["cartesia", "api_key"]).as_deref(),
            Some("several lines are a text too")
        );
        // What is no text is none: a number, a truth, a table, what is not there.
        assert_eq!(text_at(&file, &["elevenlabs", "speed"]), None);
        assert_eq!(text_at(&file, &["whisper", "enabled"]), None);
        assert_eq!(text_at(&file, &["tts", "agents"]), None);
        assert_eq!(text_at(&file, &["deepgram", "api_key"]), None);
        assert_eq!(text_at(&file, &["server", "port", "deeper"]), None);
        assert_eq!(text_at(&file, &[]), None);
        // A table of names and texts, in the owner's order, written either way.
        assert_eq!(
            texts_at(&file, &["gemini_tts", "voices"]),
            [
                ("Maximus".to_string(), "Charon".to_string()),
                ("Ünï".to_string(), "Aoede".to_string())
            ]
        );
        assert_eq!(
            texts_at(&file, &["gemini_tts", "vertex_voices"]),
            [("Maximus".to_string(), "Charon-v".to_string())]
        );
        assert!(texts_at(&file, &["nowhere", "voices"]).is_empty());
    }

    #[test]
    fn a_key_is_read_from_the_file_each_time_it_is_needed() {
        let dir = std::env::temp_dir().join(format!("clarp-owner-file-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        std::fs::write(&path, FILE).unwrap();
        let file = OwnerFile::new(Some(path.clone()));
        assert_eq!(file.key("vertex").as_deref(), Some("vertex-key"));
        assert_eq!(
            file.key("cartesia").as_deref(),
            Some("several lines are a text too")
        );
        assert_eq!(
            file.key("nobody"),
            None,
            "a provider this Host does not know"
        );
        // The owner changes a key: what is read next is the new one.
        std::fs::write(&path, "[elevenlabs]\napi_key = \"changed\"\n").unwrap();
        assert_eq!(file.key("elevenlabs").as_deref(), Some("changed"));
        // A file that is no TOML says nothing, and neither does none at all.
        std::fs::write(&path, "[elevenlabs\napi_key = ").unwrap();
        assert!(file.read().is_none());
        assert!(OwnerFile::new(None).read().is_none());
        assert!(
            OwnerFile::new(Some(dir.join("not-there.toml")))
                .read()
                .is_none()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
