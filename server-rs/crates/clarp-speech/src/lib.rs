//! Speech: what an agent says aloud, and what a person says to it.
//!
//! An agent marks what is for the ear. [`markup`] reads that marking: which parts of a reply
//! are spoken, how each is handed to an engine, and what the same reply looks like on a
//! screen, where none of the marking may show.
//!
//! What a person says reaches an agent as text. [`listen`] is the port to the engines that
//! turn a clip of speech into text; [`scribe`] is one reached over the network, [`whisper`]
//! one the Host runs itself.

pub mod listen;
pub mod markup;
pub mod scribe;
pub mod whisper;
