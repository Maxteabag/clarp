//! The changes the Python Host has a route for under doors of which this Host has built only
//! a part, or nothing: signing a tool in and out (`/backend-auth/`), signing a harness in to a
//! provider of models (`/harness-providers/`), the old kind of goal (`/agent-goal`) and the
//! Oracle (`/oracle/`).
//!
//! An action the Python Host registers is a route there, and this Host refuses it in words as
//! not built where it has not built it. Anything else under those doors is no route there,
//! the plain 404 of its dispatcher, and it is the same here. The list is that Host's exact
//! table of changes (`Handler._POST_ROUTES`), held by its own dispatcher
//! (`tests/action_doors_vectors.rs`).

/// Every `POST` path the Python Host registers under the four doors, as it is written: the
/// path of the request byte for byte, with no `%XX` read and no slash added or taken off.
pub const REGISTERED: [&str; 22] = [
    "/backend-auth/login",
    "/backend-auth/login-code",
    "/backend-auth/logout",
    "/harness-providers/login",
    "/harness-providers/login-code",
    "/harness-providers/logout",
    "/agent-goal",
    "/agent-goal/pause",
    "/agent-goal/resume",
    "/agent-goal/clear",
    "/oracle/calls",
    "/oracle/calls/close",
    "/oracle/v2/calls",
    "/oracle/v2/calls/close",
    "/oracle/v2/calls/control",
    "/oracle/contact",
    "/oracle/voice-provider",
    "/oracle/connect",
    "/oracle/handoffs/ack",
    "/oracle/delegations",
    "/oracle/delegations/ack",
    "/oracle/delegations/cancel",
];

/// Whether the Python Host has a route for a `POST` to this path under one of the four doors.
pub fn registered(path: &str) -> bool {
    REGISTERED.contains(&path)
}
