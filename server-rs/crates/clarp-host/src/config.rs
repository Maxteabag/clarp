//! What a Host is told when it starts.
//!
//! Every part that has more than one implementation is chosen here by name: the engine that
//! stores the logs, the compressor, the store of the files agents publish, the way shims are
//! started. Changing one is a restart with a different word, not a change of code.

use std::net::SocketAddr;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Where the Host keeps what it must not lose: `fleet.db` and `agents/`.
    pub data_dir: PathBuf,
    /// Where the shims' sockets are.
    pub run_dir: PathBuf,
    pub listen: SocketAddr,
    /// The engine for the agents' logs and the fleet's tables: `sqlite` or `memory`.
    pub engine: String,
    /// The compressor new log items are written with: `zstd` or `none`.
    pub codec: String,
    /// The store of the files agents publish: `files` (under `media/` in the data directory)
    /// or `memory`. Unless it is named, it goes with the engine: what keeps its logs in memory
    /// keeps its files there too.
    pub blobs: String,
    /// How a shim is started: `direct`, or `systemd` for a scope of its own.
    pub launcher: String,
    pub shim: PathBuf,
    /// The administrator's token. Empty: every request is accepted.
    pub auth_token: String,
    /// The Host's name, as the apps show it.
    pub name: String,
    /// Where a new agent works when nobody says.
    pub default_cwd: PathBuf,
    pub keep_spools: bool,
    /// Whether a turn whose account is out waits for one: `none`, or `follow` beside a Host
    /// that puts another login in place.
    pub accounts: clarp_kernel::recovery::Accounts,
    /// How often the logins of the agents' tools are looked at, where turns wait for an
    /// account.
    pub logins_checked_every_ms: u64,
    /// How often an agent with running background jobs looks whether somebody still runs them.
    pub jobs_checked_every_ms: u64,
    /// How often the fleet looks after the helpers nobody speaks of any more.
    pub helpers_checked_every_ms: u64,
    /// How long a helper that is working may sit idle before it counts as having reported.
    /// Nothing turns the rule off.
    pub helper_idle_after_ms: i64,
    /// How long a helper that is done stays in the list before it is put away.
    pub helper_archive_grace_ms: i64,
    /// What an installer stamped this installation with: the content of `DEPLOYED_VERSION` in
    /// the data directory. Empty for a Host that was not installed by one.
    pub deployed_version: String,
    /// The relay that carries pushes to phones, by its address. Empty: nothing is pushed.
    /// A Host that is started bare speaks to nobody outside; an installation names its relay.
    pub push_relay: String,
    /// This Host's own credential at the relay, for phones that gave it no grant of their
    /// own. Empty: only phones with a grant are reached.
    pub push_credential: String,
    /// The interpreter that runs the worker of the Host's own engine that listens: a Python
    /// that has `faster-whisper`. Empty: this Host has no engine of its own.
    pub whisper_python: String,
    /// The worker's script. Unless it is named: `whisper-worker.py` in `libexec` beside the
    /// directory the Host's program is in, where an installation puts it.
    pub whisper_worker: PathBuf,
    /// The model the Host's own engine runs, as the apps name it (`small.en`).
    pub whisper_model: String,
    /// What the worker loads for it: the same name, or a directory that holds the model.
    pub whisper_source: String,
    /// How the model's numbers are kept in memory (`int8`).
    pub whisper_compute: String,
    /// The language that is spoken to this Host's agents (`en`). Empty: the engines find it.
    pub speech_language: String,
    /// A file of the Python Host's kind in which the keys of engines reached over the
    /// network are looked up when a clip is to be heard. Nothing of it is copied.
    pub speech_keys: Option<PathBuf>,
    /// The engine that hears a clip nobody named one for: `local`, or a cloud engine's name.
    pub transcription_engine: String,
    /// Another address for ElevenLabs Scribe: a stand-in on this machine. Empty: the real one.
    pub scribe_endpoint: String,
    /// The same for the engines that speak.
    pub cartesia_endpoint: String,
    pub gemini_endpoint: String,
    /// And for Google's engine that listens: where a token is bought, and where speech is
    /// asked.
    pub google_token_endpoint: String,
    pub google_speech_endpoint: String,
    /// A stand-in for the program that decodes a clip for that engine. Empty: `ffmpeg`.
    pub chirp_decoder: String,
}

pub const USAGE: &str = "usage: clarp-host [--data DIR] [--run DIR] [--listen ADDR:PORT] \
[--engine sqlite|memory] [--codec zstd|none] [--blobs files|memory] \
[--launcher direct|systemd] [--shim PATH] \
[--name NAME] [--cwd DIR] [--keep-spools] [--jobs-checked-every-ms N] [--push-relay URL] \
[--accounts none|follow] [--logins-checked-every-ms N] \
[--helpers-checked-every-ms N] [--helper-idle-after-ms N] [--helper-archive-grace-ms N] \
[--whisper-python PATH] [--whisper-worker PATH] [--whisper-model NAME] [--whisper-source NAME|DIR] \
[--whisper-compute KIND] [--speech-language CODE] [--speech-keys FILE] [--transcription-engine NAME]
The token the apps must present is read from CLARP_HOST_TOKEN, and this Host's credential at
the push relay, when it has one, from CLARP_PUSH_CREDENTIAL. The key of an engine that listens
over the network is looked up in the file --speech-keys names, then in ELEVEN_API_KEY.";

fn home() -> PathBuf {
    std::env::var_os("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from)
}

impl Config {
    /// Reads the configuration from the command line and the environment.
    pub fn from_args(mut args: impl Iterator<Item = String>) -> Result<Self, String> {
        let data_home = std::env::var_os("XDG_DATA_HOME")
            .map_or_else(|| home().join(".local/share"), PathBuf::from);
        let mut data_dir = data_home.join("clarp-host");
        let mut run_dir = None;
        let mut listen = "127.0.0.1:7690".to_string();
        let mut engine = "sqlite".to_string();
        let mut codec = "zstd".to_string();
        let mut blobs = None;
        let mut launcher = "direct".to_string();
        let mut shim = None;
        // A Host is called what its machine is called, unless it is told otherwise.
        let mut name = std::fs::read_to_string("/proc/sys/kernel/hostname")
            .map(|name| name.trim().to_string())
            .ok()
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| "Clarp Host".to_string());
        let mut default_cwd = home();
        let mut keep_spools = false;
        let mut accounts = clarp_kernel::recovery::Accounts::None;
        let mut logins_checked_every_ms = 5_000;
        let mut jobs_checked_every_ms = 30_000;
        let mut helpers_checked_every_ms = 60_000;
        let mut helper_idle_after_ms = clarp_kernel::helpers::IDLE_AFTER_MS;
        let mut helper_archive_grace_ms = clarp_kernel::helpers::DEFAULT_ARCHIVE_GRACE_MS;
        let mut push_relay = String::new();
        let mut whisper_python = String::new();
        let mut whisper_worker = None;
        let mut whisper_model = "small.en".to_string();
        let mut whisper_source = None;
        let mut whisper_compute = "int8".to_string();
        let mut speech_language = "en".to_string();
        let mut speech_keys = None;
        let mut transcription_engine = "local".to_string();
        while let Some(arg) = args.next() {
            let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
            match arg.as_str() {
                "--data" => data_dir = PathBuf::from(value("a directory")?),
                "--run" => run_dir = Some(PathBuf::from(value("a directory")?)),
                "--listen" => listen = value("an address")?,
                "--engine" => engine = value("a name")?,
                "--codec" => codec = value("a name")?,
                "--blobs" => blobs = Some(value("a name")?),
                "--launcher" => launcher = value("a name")?,
                "--shim" => shim = Some(PathBuf::from(value("a path")?)),
                "--name" => name = value("a name")?,
                "--cwd" => default_cwd = PathBuf::from(value("a directory")?),
                "--keep-spools" => keep_spools = true,
                "--accounts" => {
                    let named = value("none or follow")?;
                    accounts = clarp_kernel::recovery::Accounts::by_name(&named)
                        .ok_or(format!("--accounts {named}: none or follow"))?;
                }
                "--push-relay" => {
                    push_relay = value("an address")?.trim_end_matches('/').to_string();
                    // What a phone is told goes over TLS, or stays on this machine.
                    let local = ["http://127.0.0.1:", "http://localhost:", "http://[::1]:"];
                    let safe = push_relay.starts_with("https://")
                        || local.iter().any(|begin| push_relay.starts_with(begin));
                    if !safe {
                        return Err(format!("--push-relay {push_relay}: an https address"));
                    }
                }
                "--logins-checked-every-ms" => {
                    logins_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--logins-checked-every-ms needs a number".to_string())?;
                }
                "--jobs-checked-every-ms" => {
                    jobs_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--jobs-checked-every-ms needs a number".to_string())?;
                }
                "--helpers-checked-every-ms" => {
                    helpers_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--helpers-checked-every-ms needs a number".to_string())?;
                }
                "--helper-idle-after-ms" => {
                    helper_idle_after_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--helper-idle-after-ms needs a number".to_string())?;
                }
                "--helper-archive-grace-ms" => {
                    helper_archive_grace_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--helper-archive-grace-ms needs a number".to_string())?;
                }
                "--whisper-python" => whisper_python = value("a path")?,
                "--whisper-worker" => whisper_worker = Some(PathBuf::from(value("a path")?)),
                "--whisper-model" => whisper_model = value("a name")?,
                "--whisper-source" => whisper_source = Some(value("a name or a directory")?),
                "--whisper-compute" => whisper_compute = value("a kind")?,
                "--speech-language" => speech_language = value("a language")?,
                "--speech-keys" => speech_keys = Some(PathBuf::from(value("a file")?)),
                "--transcription-engine" => transcription_engine = value("a name")?,
                other => return Err(format!("unknown argument {other}")),
            }
        }
        let run_dir = run_dir.unwrap_or_else(|| match std::env::var_os("XDG_RUNTIME_DIR") {
            Some(runtime) => PathBuf::from(runtime).join("clarp-host"),
            None => data_dir.join("run"),
        });
        // The shim is installed beside the Host.
        let shim = shim.unwrap_or_else(|| {
            std::env::current_exe()
                .ok()
                .and_then(|exe| exe.parent().map(|dir| dir.join("clarp-shim")))
                .unwrap_or_else(|| PathBuf::from("clarp-shim"))
        });
        // The worker of the Host's own engine is installed in `libexec`, one directory up.
        let whisper_worker = whisper_worker.unwrap_or_else(|| {
            let beside = std::env::current_exe().ok().and_then(|exe| {
                let installed = exe.parent()?.parent()?;
                Some(installed.join("libexec").join("whisper-worker.py"))
            });
            beside.unwrap_or_else(|| PathBuf::from("whisper-worker.py"))
        });
        // A stand-in for an engine over the network is on this machine, or it is none.
        let stand_in = |variable: &str| -> Result<String, String> {
            let address = std::env::var(variable).unwrap_or_default();
            let local = ["http://127.0.0.1:", "http://localhost:", "http://[::1]:"];
            if address.is_empty() || local.iter().any(|begin| address.starts_with(begin)) {
                Ok(address)
            } else {
                Err(format!("{variable} {address}: an address on this machine"))
            }
        };
        let scribe_endpoint = stand_in("CLARP_SCRIBE_ENDPOINT")?;
        let cartesia_endpoint = stand_in("CLARP_CARTESIA_ENDPOINT")?;
        let gemini_endpoint = stand_in("CLARP_GEMINI_ENDPOINT")?;
        let google_token_endpoint = stand_in("CLARP_GOOGLE_TOKEN_ENDPOINT")?;
        let google_speech_endpoint = stand_in("CLARP_GOOGLE_SPEECH_ENDPOINT")?;
        let chirp_decoder = std::env::var("CLARP_CHIRP_DECODER").unwrap_or_default();
        Ok(Self {
            run_dir,
            listen: listen
                .parse()
                .map_err(|error| format!("--listen {listen}: {error}"))?,
            blobs: blobs.unwrap_or_else(|| {
                let with_the_engine = if engine == "memory" {
                    "memory"
                } else {
                    "files"
                };
                with_the_engine.to_string()
            }),
            engine,
            codec,
            launcher,
            shim,
            auth_token: std::env::var("CLARP_HOST_TOKEN").unwrap_or_default(),
            name,
            default_cwd,
            keep_spools,
            accounts,
            logins_checked_every_ms,
            jobs_checked_every_ms,
            helpers_checked_every_ms,
            helper_idle_after_ms,
            helper_archive_grace_ms,
            deployed_version: std::fs::read_to_string(data_dir.join("DEPLOYED_VERSION"))
                .map(|version| version.trim().to_string())
                .unwrap_or_default(),
            data_dir,
            push_relay,
            push_credential: std::env::var("CLARP_PUSH_CREDENTIAL").unwrap_or_default(),
            whisper_source: whisper_source.unwrap_or_else(|| whisper_model.clone()),
            whisper_python,
            whisper_worker,
            whisper_model,
            whisper_compute,
            speech_language,
            speech_keys,
            transcription_engine,
            scribe_endpoint,
            cartesia_endpoint,
            gemini_endpoint,
            google_token_endpoint,
            google_speech_endpoint,
            chirp_decoder,
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Config, String> {
        Config::from_args(args.iter().map(|arg| (*arg).to_string()))
    }

    #[test]
    fn the_defaults_are_a_host_on_this_machine_with_sqlite_and_zstd() {
        let config = parse(&[]).unwrap();
        assert_eq!(config.listen.to_string(), "127.0.0.1:7690");
        assert_eq!(
            (config.engine.as_str(), config.codec.as_str()),
            ("sqlite", "zstd")
        );
        assert_eq!(config.launcher, "direct");
        assert_eq!(config.blobs, "files");
        assert!(config.data_dir.ends_with("clarp-host"));
        assert!(config.shim.ends_with("clarp-shim"));
    }

    #[test]
    fn every_choice_is_a_word_on_the_command_line() {
        let config = parse(&[
            "--data",
            "/d",
            "--run",
            "/r",
            "--listen",
            "0.0.0.0:9",
            "--engine",
            "memory",
            "--codec",
            "none",
            "--launcher",
            "systemd",
            "--shim",
            "/s",
            "--name",
            "Test",
            "--cwd",
            "/w",
            "--keep-spools",
        ])
        .unwrap();
        assert_eq!(config.data_dir, PathBuf::from("/d"));
        assert_eq!(config.run_dir, PathBuf::from("/r"));
        assert_eq!(config.listen.port(), 9);
        assert_eq!(
            (config.engine.as_str(), config.codec.as_str()),
            ("memory", "none")
        );
        assert_eq!(config.launcher, "systemd");
        assert_eq!(config.shim, PathBuf::from("/s"));
        // What keeps its logs in memory keeps its files there, unless it is told otherwise.
        assert_eq!(config.blobs, "memory");
        assert_eq!(
            parse(&["--engine", "memory", "--blobs", "files"])
                .unwrap()
                .blobs,
            "files"
        );
        assert_eq!(parse(&["--blobs", "memory"]).unwrap().blobs, "memory");
        assert!(config.keep_spools);
        assert!(parse(&["--what"]).is_err());
        assert!(parse(&["--data"]).is_err());
        assert!(parse(&["--listen", "nowhere"]).is_err());
        // A Host pushes nothing until it is given a relay, and only one it reaches over TLS
        // or on this machine.
        assert_eq!(config.push_relay, "");
        assert_eq!(
            parse(&["--push-relay", "https://relay.example/"])
                .unwrap()
                .push_relay,
            "https://relay.example"
        );
        assert!(parse(&["--push-relay", "http://127.0.0.1:9"]).is_ok());
        assert!(parse(&["--push-relay", "http://relay.example"]).is_err());
    }
}
