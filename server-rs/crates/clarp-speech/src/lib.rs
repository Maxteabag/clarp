//! Speech: what an agent says aloud, and what a person says to it.
//!
//! An agent marks what is for the ear. [`markup`] reads that marking: which parts of a reply
//! are spoken, how each is handed to an engine, and what the same reply looks like on a
//! screen, where none of the marking may show.
//!
//! What a person says reaches an agent as text. [`listen`] is the port to the engines that
//! turn a clip of speech into text; [`scribe`] and [`chirp`] are reached over the network,
//! [`whisper`] is one the Host runs itself.
//!
//! What an agent says aloud reaches a person as sound. [`speak`] is the port to the engines
//! that turn a line of text into audio, and the rule by which the next one is tried;
//! [`gemini`] and [`cartesia`] are two of them.

pub mod cartesia;
pub mod chirp;
pub mod gemini;
pub mod listen;
pub mod markup;
pub mod scribe;
pub mod speak;
pub mod whisper;
