//! Construct commands with the Host's child environment before the caller
//! supplies its own arguments, I/O and environment overrides.
use std::ffi::{OsStr, OsString};
use std::process::Command;

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
        (clarp_backend::SESSION_ENV, Clear), // Agent named to provider hooks and skills.
        ("CLARP_SESSION", Clear),            // Agent calling a Clarp tool.
        (crate::turn_identity::TURN_ENV, Clear), // Host-issued identity of the calling turn.
        ("CLARP_BACKGROUND_ORIGIN", Clear),  // Originating native Bash invocation.
        (clarp_backend::MANAGED_CLAUDE_ENV, Clear), // Rust-managed Claude process.
        ("CLARP_AGY_MANAGED_TURN", Clear),   // Managed Antigravity stream owns its callbacks.
        ("CLARP_BACKGROUND_WORKER_PID", Clear), // Worker speaking for a registered job.
        ("CLAUDE_CODE_SESSION_ID", Clear),   // Native Claude session used by adopt and status-fork.
        ("CLARP_CODE_ROOT", Clear),          // Python code a Clarp tool would import.
        ("CLAUDE_PWA_DB", Clear),            // Python Host database.
        ("CLAUDE_PWA_LOG_DIR", Clear),       // Python Host logs.
        ("CLARP_CACHE_DIR", Clear),          // Python Host cache.
        ("CLARP_DATA_DIR", Clear),           // Python Host data.
        ("CLAUDE_PWA_CONFIG", FromHost),     // Config file used by a tool to locate this Host.
        ("CLARP_CONFIG_DIR", FromHost),      // Config directory used if no file was named.
        ("CLARP_SHARE_DIR", FromHost),       // Installation whose wrappers serve this Host.
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
fn environment(
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

fn prepared(program: impl AsRef<OsStr>, keeps: Option<&[&str]>) -> Command {
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

pub(crate) fn command(program: impl AsRef<OsStr>) -> Command {
    prepared(program, None)
}

pub(crate) fn asynchronous(program: impl AsRef<OsStr>) -> tokio::process::Command {
    command(program).into()
}

pub(crate) fn one_off(
    program: impl AsRef<OsStr>,
    keeps: Option<&[&str]>,
) -> tokio::process::Command {
    prepared(program, keeps).into()
}

pub(crate) fn terminal(program: impl AsRef<OsStr>) -> pty_process::Command {
    let mut command = pty_process::Command::new(program);
    for (name, value) in environment(None) {
        command = match value {
            Some(value) => command.env(name, value),
            None => command.env_remove(name),
        };
    }
    command
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn child_orders_apply_the_table_before_the_callers_environment() {
        const TEST: &str =
            "child::tests::child_orders_apply_the_table_before_the_callers_environment";
        const REMOVED: &[&str] = &[
            "CLAUDE_PWA_SESSION",
            "CLARP_SESSION",
            "CLARP_PROVIDER_TURN",
            "CLARP_BACKGROUND_ORIGIN",
            "CLARP_RUST_MANAGED_TURN",
            "CLARP_AGY_MANAGED_TURN",
            "CLARP_BACKGROUND_WORKER_PID",
            "CLAUDE_CODE_SESSION_ID",
            "CLARP_CODE_ROOT",
            "CLAUDE_PWA_DB",
            "CLAUDE_PWA_LOG_DIR",
            "CLARP_CACHE_DIR",
            "CLARP_DATA_DIR",
            "CLARP_HOST_TOKEN",
            "CLARP_PUSH_CREDENTIAL",
            "CLARP_SOURCE_TOKEN",
            "CLARP_PEER_TOKEN",
        ];
        if std::env::var_os("FIXTURE_CHILD_ORDER").is_none() {
            let mut parent = Command::new(std::env::current_exe().unwrap());
            parent
                .args(["--exact", TEST])
                .env("FIXTURE_CHILD_ORDER", "yes");
            for key in REMOVED {
                parent.env(key, "foreign");
            }
            parent
                .env("CLARP_SHARE_DIR", "/fixture/share")
                .env("CLARP_CONFIG_DIR", "")
                .env_remove("CLAUDE_PWA_CONFIG")
                .env("CLARP_QA_PROVIDER_ROOT", "fixture");
            assert!(parent.status().unwrap().success());
            return;
        }

        // The unrestricted order explicitly clears inherited identities and
        // secrets. Configured empty and absent paths have distinct launch orders.
        let plain = command("fixture");
        let env: std::collections::BTreeMap<_, _> = plain.get_envs().collect();
        for key in REMOVED {
            assert_eq!(env.get(OsStr::new(key)), Some(&None), "{key}");
        }
        assert_eq!(
            env.get(OsStr::new("CLARP_SHARE_DIR")),
            Some(&Some(OsStr::new("/fixture/share")))
        );
        assert_eq!(
            env.get(OsStr::new("CLARP_CONFIG_DIR")),
            Some(&Some(OsStr::new("")))
        );
        assert_eq!(env.get(OsStr::new("CLAUDE_PWA_CONFIG")), Some(&None));
        assert!(!env.contains_key(OsStr::new("CLARP_QA_PROVIDER_ROOT")));

        // An allowlisted startup path and ordinary variable survive. The
        // caller then supplies its own session and can override a startup path.
        let mut restricted = one_off(
            "/bin/sh",
            Some(&[
                "CLARP_SHARE_DIR",
                "CLARP_QA_PROVIDER_ROOT",
                "CLARP_HOST_TOKEN",
                "CLARP_SESSION",
            ]),
        )
        .into_std();
        restricted.env("CLARP_SESSION", "its-turn");
        let read = r#"printf '%s|%s|%s|%s|%s|%s' "$CLARP_SHARE_DIR" "$CLARP_QA_PROVIDER_ROOT" "$CLARP_SESSION" "${CLARP_HOST_TOKEN+present}" "${CLARP_CONFIG_DIR+present}" "${FIXTURE_CHILD_ORDER+present}""#;
        let before = restricted.args(["-c", read]).output().unwrap();
        assert!(before.status.success());
        assert_eq!(before.stdout, b"/fixture/share|fixture|its-turn|||");
        restricted.env("CLARP_SHARE_DIR", "/fixture/override");
        let after = restricted.output().unwrap();
        assert!(after.status.success());
        assert_eq!(after.stdout, b"/fixture/override|fixture|its-turn|||");
    }
}
