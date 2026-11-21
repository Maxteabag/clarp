//! What this Host tells the tools of its agents, worked out anew for every message.
//!
//! The words and the rules are the kernel's ([`clarp_kernel::told`]). What they are worked
//! out from changes while the Host runs: the agent itself (its name, how it speaks, how
//! much it says while it works), the owner's settings (whether agents are told how they
//! speak at all, how human they sound) and the owner's file (which engine speaks for the
//! agent). So nothing of it is kept. An agent's thread asks here when a message arrives,
//! and the turn that message starts is told what holds then.

use std::collections::BTreeMap;
use std::sync::{Arc, PoisonError, RwLock};

use clarp_kernel::said::Who;
use clarp_kernel::told::{self, Humanness, Sound, Told};

use crate::fleet::AgentRecord;
use crate::owner::{self, OwnerFile};

type Settings = Arc<BTreeMap<String, String>>;

/// What is needed to work out what an agent's tool is told.
#[derive(Clone, Default)]
pub struct Telling {
    owner: OwnerFile,
    /// The owner's settings as the fleet last published them.
    settings: Arc<RwLock<Settings>>,
}

/// Whether agents are told how they speak, by what the owner's settings keep for it: they
/// are, unless it was turned off.
pub fn personalities(stored: Option<&str>) -> bool {
    stored != Some("0")
}

impl Telling {
    pub fn new(owner: OwnerFile) -> Self {
        Self {
            owner,
            settings: Arc::default(),
        }
    }

    /// The fleet says what the owner's settings are now.
    pub fn settings_are(&self, settings: Settings) {
        *self
            .settings
            .write()
            .unwrap_or_else(PoisonError::into_inner) = settings;
    }

    /// What the owner's file names for personas, as it is now: voices of three engines,
    /// each a table in the file's order.
    pub fn owner_tables(&self) -> [Vec<(String, String)>; 3] {
        self.owner
            .read()
            .map_or_else(Default::default, |file| owner::personas(&file))
    }

    /// What the tool of this agent is told from its next turn on.
    pub fn of(&self, record: &AgentRecord) -> Told {
        let settings = self
            .settings
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let stored = |key: &str| settings.get(key).map(String::as_str);
        let humanness = Humanness::stored(
            stored(told::HUMANNESS_KEY),
            stored(told::HUMANNESS_OF_AGENTS_KEY),
        );
        // Which engine speaks for it, by the owner's file as it is now: only one of them
        // acts out a tag of emotion, and any other would read it aloud.
        let tags = self.owner.read().is_some_and(|file| {
            let who = Who {
                agent_id: &record.agent_id,
                name: &record.persona,
                persona: &record.persona,
                session: &record.session,
            };
            owner::voices(&file).route(who).provider == "gemini"
        });
        Told {
            persona: record.persona.clone(),
            identity: told::who(
                &record.persona,
                &record.personality,
                personalities(stored(told::PERSONALITIES_KEY)),
            ),
            sound: Sound {
                humanness: humanness.of(&[record.persona.as_str(), record.session.as_str()]),
                tags,
                narration: record.narration,
            },
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn agent(persona: &str, session: &str) -> AgentRecord {
        AgentRecord {
            persona: persona.to_string(),
            session: session.to_string(),
            ..AgentRecord::default()
        }
    }

    fn settings(pairs: &[(&str, &str)]) -> Settings {
        let pairs = pairs.iter();
        Arc::new(
            pairs
                .map(|(key, text)| ((*key).to_string(), (*text).to_string()))
                .collect(),
        )
    }

    #[test]
    fn an_agent_is_told_by_what_holds_when_it_is_asked() {
        let dir = std::env::temp_dir().join(format!("clarp-telling-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("owner.toml");
        let telling = Telling::new(OwnerFile::new(Some(file.clone())));

        // Nothing is set and there is no file: its name, the way the persona of that name
        // speaks, the usual level, no tags.
        let mike = agent("Mike", "mike-1");
        let told = telling.of(&mike);
        assert_eq!(told.persona, "Mike");
        let identity = told.identity.unwrap();
        assert!(
            identity.starts_with("You are Mike. Personality: "),
            "{identity}"
        );
        assert_eq!(told.sound, Sound::default());

        // The owner's file names the engine that acts out tags; the settings give the Host
        // a level and this agent one of its own, by its session name; the agent has a way
        // of speaking of its own and says more while it works.
        std::fs::write(&file, "[tts]\nprovider = \"gemini\"\n").unwrap();
        telling.settings_are(settings(&[
            (told::HUMANNESS_KEY, "2"),
            (told::HUMANNESS_OF_AGENTS_KEY, r#"{"mike-1": 9}"#),
        ]));
        let own = AgentRecord {
            personality: "Personality: dry.".to_string(),
            narration: 2,
            ..mike.clone()
        };
        let told = telling.of(&own);
        assert_eq!(
            told.identity.as_deref(),
            Some("You are Mike. Personality: dry.")
        );
        let sound = Sound {
            humanness: 9,
            tags: true,
            narration: 2,
        };
        assert_eq!(told.sound, sound);
        assert_eq!(telling.of(&agent("Zed", "zed")).sound.humanness, 2);

        // An engine of its own for one agent, in the owner's file.
        let text = "[tts]\nprovider = \"cartesia\"\n[tts.agents.zed]\nprovider = \"gemini\"\n";
        std::fs::write(&file, text).unwrap();
        assert!(!telling.of(&mike).sound.tags);
        assert!(telling.of(&agent("Zed", "zed")).sound.tags);

        // Personalities turned off: the name and nothing more.
        telling.settings_are(settings(&[(told::PERSONALITIES_KEY, "0")]));
        assert_eq!(telling.of(&own).identity.as_deref(), Some("You are Mike."));
        assert_eq!(telling.of(&own).sound.humanness, told::HUMANNESS_USUAL);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
