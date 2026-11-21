//! What a process a test starts is given of the environment: nothing of the machine the tests
//! run on. Its home and its folders are inside the test's own place, its path holds the
//! system's utilities and none of the runner's own, its language and its time zone are
//! constants, and every program a person installs that it could start is a path that is not
//! there. Nothing of the runner's own environment comes through, unless a test names it on
//! purpose ([`sealed`]'s `named`). A test that wants a stand-in names it in what it adds, which
//! goes over all of this.
//!
//! Built from nothing, never as the runner's environment less what is known to be harmful: the
//! Host reads names that only a file tells it at run time (the variables OpenCode's catalogue
//! says a provider's key is in), and no list of names can hold those.
//!
//! One definition for every crate whose tests start a process (the Host's and the shim's), in a
//! crate of its own that nothing but tests depends on: the shim's tests cannot reach the Host's
//! test harness, and a copy in each would drift.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::Command;

/// Where a process a test starts finds a program by its name: the system's own utilities
/// (`sh` and `env` for a stand-in script, `python3` for one in Python, `git`, `timeout`), never
/// the runner's path, which is where a person's tools are installed.
pub const PATH: &str = "/usr/bin:/bin";

/// The language of every process a test starts, whatever the machine speaks.
pub const LANG: &str = "C.UTF-8";

/// The time zone of every process a test starts, wherever the machine is.
pub const TZ: &str = "UTC";

/// The home of a process a test starts, inside the test's place.
pub fn home(place: &Path) -> PathBuf {
    place.join("home")
}

/// Where a program the test did not name is looked for: a folder that is never made.
pub fn absent(place: &Path) -> PathBuf {
    place.join("absent")
}

/// The environment a process a test starts in `place` is given. `programs`: every program a
/// person installs that the process could start, by the variable that names it instead and the
/// name it is otherwise found by on the path; each is pinned to a path in [`absent`]. `named`:
/// what of the `runner`'s environment a test takes on purpose (one that runs a real tool when
/// asked to), as the runner has it; a name the runner lacks is taken from the default too.
pub fn sealed(
    runner: &[(OsString, OsString)],
    place: &Path,
    programs: &[(&str, &str)],
    named: &[&str],
) -> BTreeMap<OsString, OsString> {
    let mut given = BTreeMap::new();
    let home = home(place);
    let mut set = |name: &str, value: OsString| given.insert(OsString::from(name), value);
    set("PATH", PATH.into());
    set("LANG", LANG.into());
    set("TZ", TZ.into());
    set("HOME", home.clone().into());
    set("XDG_CONFIG_HOME", home.join(".config").into());
    set("XDG_DATA_HOME", home.join(".local/share").into());
    set("XDG_STATE_HOME", home.join(".local/state").into());
    set("XDG_CACHE_HOME", home.join(".cache").into());
    set("XDG_RUNTIME_DIR", place.join("user-run").into());
    set("TMPDIR", place.join("tmp").into());
    for (variable, name) in programs {
        set(variable, absent(place).join(name).into());
    }
    for name in named {
        match runner.iter().find(|(own, _)| own == OsStr::new(name)) {
            Some((_, value)) => given.insert(OsString::from(name), value.clone()),
            None => given.remove(OsStr::new(name)),
        };
    }
    given
}

/// A command whose whole environment is `given`: nothing it does not name is inherited.
pub fn command(program: impl AsRef<OsStr>, given: BTreeMap<OsString, OsString>) -> Command {
    let mut command = Command::new(program);
    command.env_clear().envs(given);
    command
}

/// The runner's environment, as the tests run.
pub fn runner() -> Vec<(OsString, OsString)> {
    std::env::vars_os().collect()
}

/// What the Host's crates read of the environment that [`sealed`] does not set, each with what
/// kind of thing it is: absent, because nothing but a test's own world gives it. The scan in the
/// Host's tests (`tests/host/harness.rs`) holds this list to the sources.
pub const ABSENT: &[(&str, &str)] = &[
    // Where a person's tools keep their logins and stores: the default home holds none.
    ("CLAUDE_CONFIG_DIR", "home"),
    ("CLAUDE_CONFIG_FILE", "home"),
    ("CODEX_HOME", "home"),
    ("CLAUDE_PWA_OPENCODE_HOME", "home"),
    ("CLAUDE_PWA_GROK_HOME", "home"),
    ("CLAUDE_PWA_MUSE_HOME", "home"),
    ("CLAUDE_PWA_CONFIG", "home"),
    ("CLAUDE_PWA_DB", "home"),
    ("CLAUDE_PWA_LOG_DIR", "home"),
    ("CLARP_CONFIG_DIR", "home"),
    ("CLARP_SHARE_DIR", "home"),
    ("CLARP_CACHE_DIR", "home"),
    ("CLARP_DATA_DIR", "home"),
    ("CLARP_CODE_ROOT", "home"),
    ("CLARP_TTS_ADAPTERS_DIR", "home"),
    ("CLARP_DELEGATION_DIR", "home"),
    ("CLARP_DELEGATION_TOKEN_FILE", "home"),
    ("CLARP_ORACLE_VOICE_CONTEXT_FILE", "home"),
    ("CLARP_QA_PROVIDER_ROOT", "home"),
    ("CLARP_CLAUDE_SKILLS", "home"),
    ("CLARP_CODEX_SKILLS", "home"),
    ("CLARP_AGY_SKILLS", "home"),
    ("USER", "home"),
    ("LOGNAME", "home"),
    // A provider's key or credential.
    ("OPENAI_API_KEY", "key"),
    ("GEMINI_API_KEY", "key"),
    ("GOOGLE_VERTEX_API_KEY", "key"),
    ("ELEVEN_API_KEY", "key"),
    ("DEEPGRAM_API_KEY", "key"),
    ("CARTESIA_API_KEY", "key"),
    ("TYPESAFE_API_KEY", "key"),
    ("GOOGLE_APPLICATION_CREDENTIALS", "key"),
    // Where a provider or an engine is: a stand-in only on this machine.
    ("CLARP_APNS_ENDPOINT", "endpoint"),
    ("CLARP_CARTESIA_ENDPOINT", "endpoint"),
    ("CLARP_DEEPGRAM_VOICE_ENDPOINT", "endpoint"),
    ("CLARP_ELEVEN_VOICE_ENDPOINT", "endpoint"),
    ("CLARP_FCM_ENDPOINT", "endpoint"),
    ("CLARP_FLUX_ENDPOINT", "endpoint"),
    ("CLARP_GEMINI_ENDPOINT", "endpoint"),
    ("CLARP_GEMINI_LIVE_ENDPOINT", "endpoint"),
    ("CLARP_GOOGLE_SPEECH_ENDPOINT", "endpoint"),
    ("CLARP_GOOGLE_TOKEN_ENDPOINT", "endpoint"),
    ("CLARP_GOOGLE_VOICE_ENDPOINT", "endpoint"),
    ("CLARP_INK_ENDPOINT", "endpoint"),
    ("CLARP_JUDGE_ENDPOINT", "endpoint"),
    ("CLARP_LIVE_ENDPOINT", "endpoint"),
    ("CLARP_NOVA_ENDPOINT", "endpoint"),
    ("CLARP_PICTURE_ENDPOINT", "endpoint"),
    ("CLARP_RELAY_DIAL", "endpoint"),
    ("CLARP_ROUTER_ENDPOINT", "endpoint"),
    ("CLARP_SCRIBE_ENDPOINT", "endpoint"),
    ("CLARP_USAGE_LOOPBACK", "endpoint"),
    ("CODEX_CHATGPT_BASE_URL", "endpoint"),
    ("HTTP_PROXY", "endpoint"),
    ("HTTPS_PROXY", "endpoint"),
    ("ALL_PROXY", "endpoint"),
    ("NO_PROXY", "endpoint"),
    ("https_proxy", "endpoint"),
    ("http_proxy", "endpoint"),
    ("all_proxy", "endpoint"),
    ("no_proxy", "endpoint"),
    ("SSL_CERT_FILE", "endpoint"),
    ("SSL_CERT_DIR", "endpoint"),
    // A program of the machine's that is not a person's tool.
    ("CLARP_CHIRP_DECODER", "program"),
    ("SHELL", "program"),
    // Who is calling, and the tokens a caller presents: the test's own, or none.
    ("CLARP_HOST_ADDR", "caller"),
    ("CLARP_HOST_TOKEN", "caller"),
    ("CLARP_PEER_TOKEN", "caller"),
    ("CLARP_SOURCE_TOKEN", "caller"),
    ("CLARP_FILES_TOKEN", "caller"),
    ("CLARP_TOOL_TOKEN", "caller"),
    ("CLARP_PUSH_CREDENTIAL", "caller"),
    ("CLARP_SESSION", "caller"),
    ("CLAUDE_PWA_SESSION", "caller"),
    ("CLAUDE_CODE_SESSION_ID", "caller"),
    ("CLARP_PROVIDER_TURN", "caller"),
    ("CLARP_BACKGROUND_ORIGIN", "caller"),
    ("CLARP_BACKGROUND_WORKER_PID", "caller"),
    ("CLARP_RUST_MANAGED_TURN", "caller"),
    ("CLARP_AGY_MANAGED_TURN", "caller"),
    ("CLARP_SHIM_SOCKET", "caller"),
    ("DBUS_SESSION_BUS_ADDRESS", "caller"),
    // A switch: off unless a test turns it on.
    ("CLARP_HEARTBEATS_DISABLED", "switch"),
    ("CLARP_OPENCODE_LIVE", "switch"),
    ("CLARP_IMAGE_VERSION", "switch"),
    ("CLARP_SIGN_IN_MOST_MS", "switch"),
    ("CLARP_SIGN_IN_TIMEOUT_MS", "switch"),
    ("OPENCODE_PERMISSION", "switch"),
    ("LC_ALL", "switch"),
    ("TERM", "switch"),
    ("FIXTURE_CHILD_ORDER", "switch"),
    ("FIXTURE_LAUNCH_PARENT", "switch"),
    ("FIXTURE_SPEECH_PARENT", "switch"),
];

/// Names shaped like a variable in a file that reads the environment that are not read from it:
/// a marker, a word of a protocol, a file's name.
pub const NOT_READ: &[&str] = &[
    "AGENT",
    "AGY_OK",
    "AGY_TOOL_OK",
    "ASKED_AS",
    "CLARP",
    "CLARP_",
    "CLARP_X",
    "CLARP_Y",
    "CLAUDE_PWA_",
    "CLAUDE_PWA_EXTRA",
    "DELETE",
    "DEPLOYED_VERSION",
    "DISABLED",
    "DONE",
    "ENABLED",
    "GET",
    "INGRID",
    "LS",
    "POST",
    "SIGKILL",
    "SIGTERM",
    "SIGUSR1",
    "SUCCESS",
];
