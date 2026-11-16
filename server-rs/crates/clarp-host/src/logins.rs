//! Following the logins of the agents' tools.
//!
//! On a machine where someone else puts another account in place when one is out (a Host
//! beside this one that runs the owner's selector), a turn of this Host whose account is out
//! waits (`clarp_kernel::recovery::Accounts::Follow`). It is tried again now and then; and at
//! once when the tool's login has changed, which is what this notices. It reads the file a
//! tool keeps its login in and nothing else, changes nothing, and keeps of the file only a
//! digest, in memory: a login is never written anywhere and never said.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use clarp_kernel::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::fleet::FleetHandle;

/// What stands for a login file as it is now: nothing of what is in it. `None` when the file
/// is not there or cannot be read.
fn digest(path: &Path) -> Option<[u8; 32]> {
    std::fs::read(path)
        .ok()
        .map(|bytes| Sha256::digest(&bytes).into())
}

/// The login files of the tools this Host can run, by the tool's name.
pub fn files(home: &Path) -> BTreeMap<&'static str, PathBuf> {
    ["claude", "codex"]
        .into_iter()
        .filter_map(|name| {
            let file = clarp_backend::by_id(name)?.login_file(home)?;
            Some((name, file))
        })
        .collect()
}

/// The logins as they were last seen.
#[derive(Debug, Default)]
pub struct Seen {
    by_tool: BTreeMap<&'static str, Option<[u8; 32]>>,
}

impl Seen {
    /// Looks at the login files again. Answers the tools whose login is another one than at
    /// the last look. The first look answers none: nothing is known to have changed.
    pub fn look(&mut self, files: &BTreeMap<&'static str, PathBuf>) -> Vec<&'static str> {
        let mut changed = Vec::new();
        for (tool, file) in files {
            let now = digest(file);
            match self.by_tool.insert(*tool, now) {
                // A login that is gone has not become another one: nothing can be tried.
                Some(before) if before != now && now.is_some() => changed.push(*tool),
                _ => {}
            }
        }
        changed
    }
}

/// What a login file says of a login without saying the login: whether a token is in it,
/// and when that token runs out, in milliseconds (0: the file does not say). The token is
/// read, looked at for these two things, and dropped.
pub fn credential(tool: &str, file: &Path) -> (bool, i64) {
    let read = std::fs::read(file).ok();
    let kept: Value = read
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or(Value::Null);
    let text = |value: &Value| value.as_str().unwrap_or("").to_string();
    match tool {
        "claude" => {
            let login = &kept["claudeAiOauth"];
            let expires = login["expiresAt"].as_i64().unwrap_or(0);
            (!text(&login["accessToken"]).is_empty(), expires)
        }
        "codex" => {
            let token = match text(&kept["tokens"]["access_token"]) {
                token if token.is_empty() => text(&kept["OPENAI_API_KEY"]),
                token => token,
            };
            (!token.is_empty(), expiry_of(&token))
        }
        _ => (false, 0),
    }
}

/// When a token that says so of itself runs out, in milliseconds: the `exp` of a token in
/// three parts. 0 for any other.
fn expiry_of(token: &str) -> i64 {
    let parts: Vec<&str> = token.split('.').collect();
    let [_, payload, _] = parts.as_slice() else {
        return 0;
    };
    let standard: String = payload
        .chars()
        .map(|sign| match sign {
            '-' => '+',
            '_' => '/',
            other => other,
        })
        .collect();
    let padded = format!("{standard}{}", "=".repeat((4 - standard.len() % 4) % 4));
    let said: Value = python::b64decode(&padded)
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or(Value::Null);
    said["exp"].as_i64().unwrap_or(0).saturating_mul(1000)
}

/// Whether a program can be started: a path that is a file, or a name found on the path.
fn installed(program: &str) -> bool {
    if program.contains('/') {
        return Path::new(program).is_file();
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path).any(|dir| dir.join(program).is_file())
}

/// The tools this Host runs that a person signs in to, as an app's screen of accounts names
/// them: the tool, its name, and how one signs in to it.
const TOOLS: [(&str, &str, &str); 2] = [
    ("claude", "Claude", "cli"),
    ("codex", "Codex", "device_code"),
];

/// A tool's sign-in as an app is told it (`GET /backend-auth`), in the Python Host's fields.
/// This Host reads the file the tool keeps its login in and asks nobody: a login that is
/// there and has not run out is `unverified`, which an app shows as that. `every`: the
/// fields an app that asks for every harness is given too.
pub fn told(home: &Path, every: bool, now_ms: i64) -> Value {
    let files = files(home);
    let rows: Vec<Value> = TOOLS
        .iter()
        .map(|(tool, name, login_kind)| {
            let mut row = json!({ "id": tool, "name": name, "login_kind": login_kind });
            if every {
                // Signing in is done with the tool itself, on the machine: not from here.
                row["can_login"] = json!(false);
                row["model_providers"] = json!(false);
                row["note"] = json!("Sign in with the tool itself on this Computer.");
            }
            let program = clarp_backend::program(tool).unwrap_or_default();
            if !installed(&program) {
                row["installed"] = json!(false);
                row["logged_in"] = json!(false);
                return row;
            }
            let (present, expires_at) = files
                .get(tool)
                .map_or((false, 0), |file| credential(tool, file));
            let state = match (present, expires_at) {
                (false, _) => "signed_out",
                (true, at) if at > 0 && at <= now_ms => "expired",
                (true, _) => "unverified",
            };
            for (key, value) in [
                ("installed", json!(true)),
                ("logged_in", json!(false)),
                ("account", json!("")),
                ("method", json!("")),
                ("state", json!(state)),
                ("credential_present", json!(present)),
                ("can_logout", json!(false)),
                ("expires_at", json!(expires_at)),
                ("validation_error", json!("")),
                ("error", json!("")),
            ] {
                row[key] = value;
            }
            row
        })
        .collect();
    let idle = |tool: &str| {
        json!({
            "backend": tool, "status": "idle", "output": "", "verification_url": "",
            "user_code": "", "awaiting_code": false, "code_submitted": false,
            "started_at": now_ms, "expires_at": 0, "error": "",
        })
    };
    let tasks: serde_json::Map<String, Value> = TOOLS
        .iter()
        .map(|(tool, _, _)| ((*tool).to_string(), idle(tool)))
        .collect();
    json!({ "backends": rows, "tasks": tasks })
}

/// Looks at the logins every `every`, for as long as the Host runs, and tells the agents of
/// a tool whose login has changed.
pub fn follow(fleet: FleetHandle, home: PathBuf, every: Duration) {
    let files = files(&home);
    let spawned = std::thread::Builder::new()
        .name("logins".to_string())
        .spawn(move || {
            let mut seen = Seen::default();
            loop {
                for tool in seen.look(&files) {
                    for member in &fleet.directory().members {
                        if member.record.backend == tool {
                            member.agent.login_changed();
                        }
                    }
                }
                std::thread::sleep(every);
            }
        });
    if let Err(error) = spawned {
        eprintln!("clarp-host: the logins are not followed: {error}");
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_login_that_became_another_one_is_noticed_once() {
        let home = std::env::temp_dir().join(format!("clarp-logins-{}", std::process::id()));
        let claude = home.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        let files: BTreeMap<&'static str, PathBuf> =
            [("claude", claude.join(".credentials.json"))].into();
        let mut seen = Seen::default();
        // Nothing is there: nothing to tell. Then a login is put there: that is news.
        assert!(seen.look(&files).is_empty());
        std::fs::write(&files["claude"], b"the first account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        assert!(seen.look(&files).is_empty(), "the same login is no news");
        // Another account is put in its place.
        std::fs::write(&files["claude"], b"another account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        assert!(seen.look(&files).is_empty());
        // A login that is taken away is nothing a turn could be tried on.
        std::fs::remove_file(&files["claude"]).unwrap();
        assert!(seen.look(&files).is_empty());
        std::fs::write(&files["claude"], b"another account").unwrap();
        assert_eq!(seen.look(&files), vec!["claude"]);
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn a_login_file_says_whether_a_token_is_there_and_when_it_runs_out_and_nothing_else() {
        let home = std::env::temp_dir().join(format!("clarp-credential-{}", std::process::id()));
        std::fs::create_dir_all(home.join(".claude")).unwrap();
        std::fs::create_dir_all(home.join(".codex")).unwrap();
        let (claude, codex) = (
            home.join(".claude/.credentials.json"),
            home.join(".codex/auth.json"),
        );
        // Nothing there, and what is no login.
        assert_eq!(credential("claude", &claude), (false, 0));
        std::fs::write(&claude, b"not json").unwrap();
        assert_eq!(credential("claude", &claude), (false, 0));
        std::fs::write(
            &claude,
            br#"{"claudeAiOauth": {"accessToken": "", "expiresAt": 5}}"#,
        )
        .unwrap();
        assert_eq!(credential("claude", &claude), (false, 5));
        // A login of Claude Code says when it runs out.
        let login = br#"{"claudeAiOauth": {"accessToken": "made-up", "expiresAt": 1794840000000}}"#;
        std::fs::write(&claude, login).unwrap();
        assert_eq!(credential("claude", &claude), (true, 1_794_840_000_000));
        // A login of Codex is a token that says so of itself, or a key that does not.
        let payload = python::b64encode(br#"{"exp": 1794840000, "sub": "made-up"}"#);
        let payload = payload
            .trim_end_matches('=')
            .replace('+', "-")
            .replace('/', "_");
        let token = format!("aaaa.{payload}.cccc");
        let kept = json!({ "tokens": { "access_token": token } }).to_string();
        std::fs::write(&codex, kept).unwrap();
        assert_eq!(credential("codex", &codex), (true, 1_794_840_000_000));
        std::fs::write(&codex, br#"{"OPENAI_API_KEY": "made-up-key"}"#).unwrap();
        assert_eq!(credential("codex", &codex), (true, 0));
        assert_eq!(expiry_of("no-token"), 0);
        assert_eq!(expiry_of("a.!!!.c"), 0);
        assert_eq!(credential("scripted", &codex), (false, 0));

        // What an app is told of it: a login that is there is not called verified, one that
        // has run out is called that, and nothing of the login itself is said.
        let before = told(&home, true, 1_794_830_000_000);
        let told_of = |all: &Value, tool: &str| -> Value {
            let rows = all["backends"].as_array().unwrap();
            rows.iter().find(|row| row["id"] == tool).unwrap().clone()
        };
        let row = told_of(&before, "claude");
        assert_eq!(
            (&row["name"], &row["login_kind"], &row["can_login"]),
            (&json!("Claude"), &json!("cli"), &json!(false))
        );
        if row["installed"] == true {
            assert_eq!(
                (
                    &row["state"],
                    &row["logged_in"],
                    &row["credential_present"],
                    &row["expires_at"]
                ),
                (
                    &json!("unverified"),
                    &json!(false),
                    &json!(true),
                    &json!(1_794_840_000_000_i64)
                )
            );
            let after = told_of(&told(&home, false, 1_794_850_000_000), "claude");
            assert_eq!(after["state"], "expired");
            assert!(
                after.get("can_login").is_none(),
                "only for an app that asks for every harness"
            );
        }
        assert!(!before.to_string().contains("made-up"), "{before}");
        assert_eq!(before["tasks"]["codex"]["status"], "idle");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_tools_logins_are_where_the_tools_keep_them() {
        let found = files(Path::new("/home/someone"));
        assert_eq!(found.len(), 2);
        assert!(found["claude"].ends_with(".credentials.json"));
        assert!(found["codex"].ends_with("auth.json"));
    }
}
