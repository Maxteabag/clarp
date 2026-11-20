//! Speech: what an agent says aloud, and what a person says to it.
//!
//! An agent marks what is for the ear. [`markup`] reads that marking: which parts of a reply
//! are spoken, how each is handed to an engine, and what the same reply looks like on a
//! screen, where none of the marking may show.
//!
//! What a person says reaches an agent as text. [`listen`] is the port to the engines that
//! turn a clip of speech into text; [`scribe`], [`nova`] and [`chirp`] are reached over the
//! network, [`whisper`] is one the Host runs itself.
//!
//! What an agent says aloud reaches a person as sound. [`speak`] is the port to the engines
//! that turn a line of text into audio, and the rule by which the next one is tried;
//! [`gemini`] and [`cartesia`] are two of them.

pub mod cartesia;
pub mod chirp;
pub mod deepgram_voice;
pub mod eleven_voice;
pub mod gemini;
pub mod google_voice;
pub mod listen;
pub mod markup;
pub mod nova;
pub mod scribe;
pub mod speak;
pub mod whisper;

pub mod ink;
mod speech_socket;

/// Run the selected test with a synthetic Host secret in an isolated parent.
#[cfg(test)]
#[allow(clippy::unwrap_used)]
fn run_with_fixture_host_token(test: &str) -> bool {
    if std::env::var_os("FIXTURE_SPEECH_PARENT").is_some() {
        assert_eq!(std::env::var("CLARP_HOST_TOKEN").unwrap(), "synthetic");
        return false;
    }
    // The test again, in a child that has nothing of the machine's but what it is given.
    let out = std::process::Command::new(std::env::current_exe().unwrap())
        .env_clear()
        .args(["--exact", test])
        .env("FIXTURE_SPEECH_PARENT", "1")
        .env("CLARP_HOST_TOKEN", "synthetic")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 passed"));
    true
}
