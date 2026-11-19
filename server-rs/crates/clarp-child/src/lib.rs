//! Construct commands with the Host's child environment before the caller
//! supplies its own arguments, I/O and environment overrides.
use std::ffi::{OsStr, OsString};
use std::process::Command;

pub const SESSION_ENV: &str = "CLAUDE_PWA_SESSION";
pub const TURN_ENV: &str = "CLARP_PROVIDER_TURN";
pub const MANAGED_CLAUDE_ENV: &str = "CLARP_RUST_MANAGED_TURN";
pub const MANAGED_AGY_ENV: &str = "CLARP_AGY_MANAGED_TURN";

/// What in the Host's environment is the Host's alone. Nothing the Host starts is given these:
/// a turn's environment is read by every command an agent runs, and what an agent prints is in
/// its conversation.
pub const OWN_SECRETS: &[&str] = &[
    "CLARP_HOST_TOKEN",
    "CLARP_PUSH_CREDENTIAL",
    "CLARP_SOURCE_TOKEN",
    "CLARP_PEER_TOKEN",
];

/// The common part of the launch order, captured when the Host starts. A
/// missing value explicitly removes the name. A turn then adds its own identity.
pub fn child_environment() -> &'static [(&'static str, Option<std::ffi::OsString>)] {
    use ChildVariable::{Clear, FromHost};
    use std::sync::LazyLock;

    enum ChildVariable {
        Clear,
        FromHost,
    }
    const NAMES: &[(&str, ChildVariable)] = &[
        (SESSION_ENV, Clear),               // Agent named to provider hooks and skills.
        ("CLARP_SESSION", Clear),           // Agent calling a Clarp tool.
        (TURN_ENV, Clear),                  // Host-issued identity of the calling turn.
        ("CLARP_BACKGROUND_ORIGIN", Clear), // Originating native Bash invocation.
        (MANAGED_CLAUDE_ENV, Clear),        // Rust-managed Claude process.
        (MANAGED_AGY_ENV, Clear),           // Managed Antigravity stream owns its callbacks.
        ("CLARP_BACKGROUND_WORKER_PID", Clear), // Worker speaking for a registered job.
        ("CLAUDE_CODE_SESSION_ID", Clear),  // Native Claude session used by adopt and status-fork.
        ("CLARP_CODE_ROOT", Clear),         // Python code a Clarp tool would import.
        ("CLAUDE_PWA_DB", Clear),           // Python Host database.
        ("CLAUDE_PWA_LOG_DIR", Clear),      // Python Host logs.
        ("CLARP_CACHE_DIR", Clear),         // Python Host cache.
        ("CLARP_DATA_DIR", Clear),          // Python Host data.
        ("CLAUDE_PWA_CONFIG", FromHost),    // Config file used by a tool to locate this Host.
        ("CLARP_CONFIG_DIR", FromHost),     // Config directory used if no file was named.
        ("CLARP_SHARE_DIR", FromHost),      // Installation whose wrappers serve this Host.
    ];

    static ORDER: LazyLock<Vec<(&str, Option<OsString>)>> = LazyLock::new(|| {
        NAMES
            .iter()
            .map(|&(name, ref rule)| {
                let value = match rule {
                    Clear => None,
                    FromHost => std::env::var_os(name),
                };
                (name, value)
            })
            .collect()
    });
    &ORDER
}

/// What the child receives before the caller adds its own launch order.
pub fn environment(
    keeps: Option<&[&str]>,
) -> impl Iterator<Item = (&'static str, Option<&'static OsStr>)> {
    OWN_SECRETS
        .iter()
        .map(|&name| (name, None))
        .chain(child_environment().iter().map(move |(name, value)| {
            let allowed = keeps.is_none_or(|permitted| permitted.contains(name));
            (*name, value.as_deref().filter(|_| allowed))
        }))
}

pub fn prepared(program: impl AsRef<OsStr>, keeps: Option<&[&str]>) -> Command {
    let mut command = Command::new(program);
    if let Some(names) = keeps {
        command.env_clear();
        for name in names {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
    }
    for (name, value) in environment(keeps) {
        match value {
            Some(value) => command.env(name, value),
            None => command.env_remove(name),
        };
    }
    command
}

pub fn command(program: impl AsRef<OsStr>) -> Command {
    prepared(program, None)
}
