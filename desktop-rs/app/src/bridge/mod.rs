// cxx-qt also generates an emitter for the source model's dataChanged, which
// only Qt calls, and the bridge module accepts no attributes of its own.
#[allow(dead_code)]
pub mod agent_filter_model;
pub mod agent_list_model;
// createAgent keeps the C++ QML signature (nine arguments).
#[allow(clippy::too_many_arguments)]
pub mod controller;
pub mod conversation_model;
pub mod directory_models;
pub mod pane_tree_model;
pub mod presentation_model;
pub mod preview_versions;
pub mod tool_narrator;
