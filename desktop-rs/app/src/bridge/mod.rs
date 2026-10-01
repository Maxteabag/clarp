// cxx-qt also generates an emitter for the source model's dataChanged, which
// only Qt calls, and the bridge module accepts no attributes of its own.
#[allow(dead_code)]
pub mod agent_filter_model;
pub mod agent_list_model;
pub mod audio_controller;
// applicationStateChanged is emitted by Qt through a string connection.
#[allow(dead_code)]
pub mod avatar_motion;
// createAgent keeps the C++ QML signature (nine arguments).
#[allow(clippy::too_many_arguments)]
pub mod controller;
pub mod conversation_model;
pub mod desktop;
pub mod desktop_services;
// eventLoopAwake is emitted by Qt through a string connection.
#[allow(dead_code)]
pub mod diagnostics;
pub mod directory_models;
// windowRequested carries the whole launch (seven arguments).
#[allow(clippy::too_many_arguments)]
pub mod instance_server;
pub mod key_injector;
pub mod pane_tree_model;
pub mod presentation_model;
#[allow(dead_code)]
pub mod quick;
pub mod preview_versions;
pub mod tool_narrator;
// The forwarding signals are emitted by Qt through string connections.
#[allow(dead_code)]
pub mod transcript_layout;
#[allow(dead_code)]
pub mod transcript_rows;
pub mod window_capture;
