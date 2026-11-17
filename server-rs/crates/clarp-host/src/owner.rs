//! The owner's own file of settings: a config of the Python Host's kind, read where it is.
//!
//! On a machine that has a Python Host, the owner's keys and his choices of speech are in
//! that Host's config. This Host reads them there, each time it needs one, and keeps
//! nothing: a key the owner changes is the key that is used next, and no secret is copied
//! into this Host's own files. A Host that is told of no such file has what its own
//! environment says, and nothing else.

use std::path::PathBuf;

use clarp_kernel::said::Voices;
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

/// The owner's selectors of accounts, by tool: the program that puts another login in place
/// when an account of that tool is out, with its arguments (`[agents]
/// claude_account_switch_command`, `codex_account_switch_command`, as the Python Host reads
/// them: a list of texts, none of them empty; anything else is no selector).
pub fn selectors(file: &Table) -> Vec<(&'static str, Vec<String>)> {
    let Some(agents) = file.get("agents").and_then(|agents| agents.as_table()) else {
        return Vec::new();
    };
    let named = [
        ("claude", "claude_account_switch_command"),
        ("codex", "codex_account_switch_command"),
    ];
    let mut selectors = Vec::new();
    for (tool, key) in named {
        let Some(words) = agents.get(key).and_then(|command| command.as_array()) else {
            continue;
        };
        let command: Vec<String> = words
            .iter()
            .filter_map(|word| word.as_str())
            .filter(|word| !word.is_empty())
            .map(str::to_string)
            .collect();
        if !command.is_empty() && command.len() == words.len() {
            selectors.push((tool, command));
        }
    }
    selectors
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

/// A value of the file as the text the Python Host makes of it: a text as it is, a number
/// or a truth as it is written.
fn as_text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.trim().to_string()),
        Value::Integer(number) => Some(number.to_string()),
        Value::Float(number) => Some(number.to_string()),
        Value::Boolean(truth) => Some(if *truth { "True" } else { "False" }.to_string()),
        _ => None,
    }
}

/// What the owner's file adds to the personas this Host has built in, each a table of texts:
/// a voice of ElevenLabs for a name (`[roster]`, which also makes a name of his own a
/// persona), a voice of Cartesia (`[cartesia.voices]`) and one of Gemini
/// (`[gemini_tts.voices]`).
pub fn personas(file: &Table) -> [Vec<(String, String)>; 3] {
    [
        texts_at(file, &["roster"]),
        texts_at(file, &["cartesia", "voices"]),
        texts_at(file, &["gemini_tts", "voices"]),
    ]
}

/// The owner's choices of speech out as his file has them: the engine every agent speaks
/// with (`[tts] provider`, Cartesia unless he says otherwise) and the one that is tried
/// after it (`fallback`), the entries of single agents (`[tts.agents.<name>]`), Gemini's
/// voices for his personas behind each of Google's doors (`[gemini_tts] voices`,
/// `vertex_voices`) and its voice for all (`voice`), and his own voices of Cartesia's over
/// the ones the Python Host ships with (`[cartesia.voices]`).
pub fn voices(file: &Table) -> Voices {
    let text = |path: &[&str]| text_at(file, path);
    let lower = |text: Option<String>, otherwise: &str| {
        text.map_or_else(|| otherwise.to_string(), |text| text.to_lowercase())
    };
    let named = |path: &[&str]| -> Vec<(String, String)> {
        let listed = texts_at(file, path).into_iter();
        listed.filter(|(_, voice)| !voice.is_empty()).collect()
    };
    let agents = file
        .get("tts")
        .and_then(|tts| tts.get("agents"))
        .and_then(Value::as_table);
    let entries: Vec<(String, Vec<(String, String)>)> = agents
        .into_iter()
        .flatten()
        .filter_map(|(name, entry)| {
            let set = entry.as_table()?.iter();
            let set = set.filter_map(|(key, value)| Some((key.clone(), as_text(value)?)));
            Some((name.clone(), set.collect()))
        })
        .collect();
    let borrowed: Vec<(&str, Vec<(&str, &str)>)> = entries
        .iter()
        .map(|(name, entry)| {
            let set = entry
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()));
            (name.as_str(), set.collect())
        })
        .collect();
    let given: Vec<(&str, &[(&str, &str)])> = borrowed
        .iter()
        .map(|(name, entry)| (*name, entry.as_slice()))
        .collect();
    Voices {
        provider: lower(text(&["tts", "provider"]), "cartesia"),
        fallback: lower(text(&["tts", "fallback"]), "none"),
        gemini_voices: named(&["gemini_tts", "voices"]),
        gemini_vertex_voices: named(&["gemini_tts", "vertex_voices"]),
        gemini_voice: text(&["gemini_tts", "voice"]).unwrap_or_else(|| "Kore".to_string()),
        ..Voices::default()
    }
    .with_agents(&given)
    .with_cartesia(&named(&["cartesia", "voices"]))
}

/// Which of Gemini's models speaks, and through which of Google's doors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GeminiChoice {
    pub model: String,
    /// Through Vertex AI, with its own key; otherwise through the Gemini API.
    pub vertex: bool,
}

pub fn gemini(file: &Table) -> GeminiChoice {
    let door = text_at(file, &["gemini_tts", "backend"]).unwrap_or_default();
    GeminiChoice {
        model: text_at(file, &["gemini_tts", "model"])
            .unwrap_or_else(|| "gemini-3.8-flash-tts".to_string()),
        vertex: door.to_lowercase() == "vertex",
    }
}

/// The owner's own account at Google, for the engine that listens there (`[google_stt]`):
/// the file of that account, the project that is asked and pays, and where. The file is the
/// one the owner names, else the one his environment names, else where `gcloud` puts it.
/// `None`: no project is named, or the file cannot be read: this Host has no such engine.
pub fn google_speech(file: &Table) -> Option<clarp_speech::chirp::Account> {
    let project = text_at(file, &["google_stt", "project"])?;
    let named = text_at(file, &["google_stt", "credentials"])
        .or_else(|| std::env::var("GOOGLE_APPLICATION_CREDENTIALS").ok())
        .map(|named| named.trim().to_string())
        .filter(|named| !named.is_empty());
    let home = || std::env::var_os("HOME").map(PathBuf::from);
    let credentials = match named {
        Some(named) => match named.strip_prefix("~/") {
            Some(rest) => home()?.join(rest),
            None => PathBuf::from(named),
        },
        None => home()?.join(".config/gcloud/application_default_credentials.json"),
    };
    // Readable now: a file that is there and cannot be opened is no account.
    std::fs::File::open(&credentials).ok()?;
    credentials.is_file().then(|| clarp_speech::chirp::Account {
        credentials,
        project,
        location: text_at(file, &["google_stt", "location"]).unwrap_or_else(|| "eu".to_string()),
    })
}

/// Why Google's voices cannot be used on this Host, in the Python Host's code for it
/// (`google_tts.credentials`): the account's file is missing, or no project is named. An
/// owner who wrote a `[google_tts]` section is held to it; else the account of the engine
/// that listens is the one. `None`: they could be used.
pub fn google_voice_error(file: &Table) -> Option<&'static str> {
    let home = || std::env::var_os("HOME").map(PathBuf::from);
    let readable = |named: Option<String>, otherwise: Option<PathBuf>| -> bool {
        let named = named.map(|named| named.trim().to_string());
        let path = match named.filter(|named| !named.is_empty()) {
            Some(named) => match named.strip_prefix("~/") {
                Some(rest) => home().map(|home| home.join(rest)),
                None => Some(PathBuf::from(named)),
            },
            None => otherwise,
        };
        path.is_some_and(|path| path.is_file() && std::fs::File::open(&path).is_ok())
    };
    let own = file.get("google_tts").and_then(Value::as_table);
    let (has_file, project) = match own {
        Some(_) => (
            readable(text_at(file, &["google_tts", "credentials"]), None),
            text_at(file, &["google_tts", "project"]),
        ),
        None => {
            let named = text_at(file, &["google_stt", "credentials"])
                .or_else(|| std::env::var("GOOGLE_APPLICATION_CREDENTIALS").ok());
            let usual =
                home().map(|home| home.join(".config/gcloud/application_default_credentials.json"));
            (
                readable(named, usual),
                text_at(file, &["google_stt", "project"]),
            )
        }
    };
    match (has_file, project) {
        (false, _) => Some("google_credentials_missing"),
        (true, None) => Some("google_project_missing"),
        (true, Some(_)) => None,
    }
}

/// Which of Cartesia's models speaks.
pub fn cartesia_model(file: &Table) -> String {
    text_at(file, &["cartesia", "model"]).unwrap_or_else(|| "sonic-3.5".to_string())
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
    fn the_owners_choices_of_speech_out_are_read_as_the_python_host_reads_them() {
        use clarp_kernel::said::Who;

        let file: Table = FILE.parse().unwrap();
        let chosen = voices(&file);
        assert_eq!(
            (chosen.provider.as_str(), chosen.fallback.as_str()),
            ("gemini", "cartesia")
        );
        assert_eq!(
            chosen.gemini_voice, "Kore",
            "the voice for all, unless the owner names one"
        );
        // An agent's own entry, whatever letters its name is written in.
        let ingrid = Who {
            name: "",
            persona: "INGRID",
            session: "ingrid-1",
        };
        let route = chosen.route(ingrid);
        assert_eq!(
            (
                route.provider.as_str(),
                route.voice.as_deref(),
                route.fallback.as_str()
            ),
            ("gemini", Some("Charon"), "cartesia")
        );
        let spaced = Who {
            name: "Name With Space",
            ..Who::default()
        };
        assert_eq!(
            chosen.entry_for(spaced),
            [("cartesia_voice".to_string(), "ca-1".to_string())]
        );
        // The voices of his personas behind each of Google's doors; what is no text is none.
        assert_eq!(chosen.gemini_voice_for("maximus"), Some("Charon"));
        assert_eq!(chosen.gemini_voice_for("odd"), None);
        assert_eq!(chosen.gemini_voice_at("Charon", true), "Charon-v");
        assert_eq!(chosen.gemini_voice_at("Charon", false), "Charon");
        // Cartesia's voices the Python Host ships with are there without a word of his.
        assert!(chosen.cartesia_voice_for("Rachel").is_some());
        assert_eq!(
            gemini(&file),
            GeminiChoice {
                model: "gemini-3.8-flash-tts".to_string(),
                vertex: false
            }
        );
        assert_eq!(cartesia_model(&file), "sonic-3.5");

        // A file that says nothing of speech: Cartesia, and nobody after it.
        let silent: Table = "[server]\nport = 1\n".parse().unwrap();
        let chosen = voices(&silent);
        assert_eq!(
            (chosen.provider.as_str(), chosen.fallback.as_str()),
            ("cartesia", "none")
        );
        assert!(chosen.agents.is_empty() && chosen.gemini_voices.is_empty());
        let other: Table = "[gemini_tts]\nbackend = \" Vertex \"\nmodel = \"m2\"\nvoice = \"Puck\"\n[cartesia]\nmodel = \"sonic-9\"\n[cartesia.voices]\nMaximus = \"ca-max\"\n"
            .parse()
            .unwrap();
        assert_eq!(
            gemini(&other),
            GeminiChoice {
                model: "m2".to_string(),
                vertex: true
            }
        );
        assert_eq!(cartesia_model(&other), "sonic-9");
        let chosen = voices(&other);
        assert_eq!(chosen.gemini_voice, "Puck");
        assert_eq!(chosen.cartesia_voice_for("Maximus"), Some("ca-max"));
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
