//! Qt-free core of the Clarp Rust desktop client: wire types, the SSE parser,
//! the conversation sync and delivery reducers, and transcript text rules.
//! Everything here is pure so `cargo test` needs no display or Qt runtime.

pub mod catalog;
pub mod conversation;
pub mod delivery;
pub mod directory;
pub mod endpoint;
pub mod jobs;
pub mod json;
pub mod list_ops;
pub mod narrator;
pub mod panes;
pub mod presentation;
pub mod protocol;
pub mod reading_theme;
pub mod roster;
pub mod settings;
pub mod sidebar;
pub mod sse;
pub mod sync;
pub mod text;
pub mod time_format;
pub mod tree;
pub mod workspace;
