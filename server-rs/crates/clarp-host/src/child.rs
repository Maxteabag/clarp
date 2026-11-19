//! Async and PTY adapters for the shared child command policy.
pub(crate) use clarp_child::command;
pub use clarp_child::{OWN_SECRETS, child_environment};
use clarp_child::{environment, prepared};
use std::ffi::OsStr;
#[cfg(test)]
use std::process::Command;

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
