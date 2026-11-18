//! What a Host is told when it starts.
//!
//! Every part that has more than one implementation is chosen here by name: the engine that
//! stores the logs, the compressor, the store of the files agents publish, the way shims are
//! started. Changing one is a restart with a different word, not a change of code.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

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
    /// The owner's tool that knows the seats of the machine's accounts (Hotseat): a
    /// program's name or its path. Empty: this Host was told none, reads no seat, and says
    /// that the tool is not installed.
    pub seats_tool: String,
    /// How often the logins of the agents' tools are looked at, where turns wait for an
    /// account.
    pub logins_checked_every_ms: u64,
    /// How often an agent with running background jobs looks whether somebody still runs them.
    pub jobs_checked_every_ms: u64,
    /// How often the schedules are looked at for what is due.
    pub schedules_checked_every_ms: u64,
    /// How often the heartbeat looks which agent is due.
    pub heartbeats_checked_every_ms: u64,
    /// How often the janitors' runner looks at what is due and at the runs that are out.
    pub janitors_checked_every_ms: u64,
    /// How often the Goal keeper looks at the open goals for what is due.
    pub goals_kept_every_ms: u64,
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
    /// The file in which the owner names his key at the push service (`--push-keys`), which
    /// lets this Host push a ring to a phone as an incoming call. It is a word of its own:
    /// the file of the keys of speech is read by a Host that is only being tried too, and
    /// that Host is not to push with its owner's key because the file happens to name it.
    pub push_keys: Option<PathBuf>,
    /// The directory that holds the pictures this Host ships for its agents (`avatars/` in
    /// it). `None`: it ships none, and its agents are shown without a face.
    pub static_dir: Option<PathBuf>,
    /// The engine that hears a clip nobody named one for: `local`, or a cloud engine's name.
    pub transcription_engine: String,
    /// Another address for ElevenLabs Scribe: a stand-in on this machine. Empty: the real one.
    pub scribe_endpoint: String,
    /// The same for Deepgram's engine that hears clips, and for its engine that finds the
    /// ends of turns on a stream of speech, which is a socket on this machine.
    pub nova_endpoint: String,
    pub flux_endpoint: String,
    /// The same for the voice of a ring held as a live voice call, which is a socket too.
    pub live_endpoint: String,
    /// Where the router of the Oracle's calls is asked, in a test: an address on this machine.
    pub router_endpoint: String,
    /// The same for the engines that speak.
    pub cartesia_endpoint: String,
    /// A stand-in for the push service on this machine, for a test (`CLARP_APNS_ENDPOINT`).
    /// Empty: the push service itself.
    pub apns_endpoint: String,
    pub fcm_endpoint: Option<String>,
    pub gemini_endpoint: String,
    /// And for Google's engine that listens: where a token is bought, and where speech is
    /// asked.
    pub google_token_endpoint: String,
    pub google_speech_endpoint: String,
    /// A stand-in for the program that decodes a clip for that engine. Empty: `ffmpeg`.
    pub chirp_decoder: String,
    /// A file of the Python Host's kind that says whether this Host is reached through the
    /// connection relay (`[network] relay_enabled`); the relay's name for the Host and its
    /// key are beside it, in `relay.json`. `None`: this Host dials no relay. It is its own
    /// word and nothing else's: a Host that reads the owner's keys from the Python Host's
    /// file does not thereby take that Host's name at the relay.
    pub relay_config: Option<PathBuf>,
    /// A stand-in for the relay, on this machine. Empty: the relay the settings name.
    pub relay_stand_in: String,
    /// No agent of this Host is woken by a heartbeat, whatever its own switch says, and a
    /// heartbeat sent from outside is refused (`CLARP_HEARTBEATS_DISABLED`, as on the
    /// Python Host).
    pub heartbeats_disabled: bool,
}

pub const USAGE: &str = "usage: clarp-host [--data DIR] [--run DIR] [--listen ADDR:PORT] \
[--engine sqlite|memory] [--codec zstd|none] [--blobs files|memory] \
[--launcher direct|systemd] [--shim PATH] \
[--name NAME] [--cwd DIR] [--keep-spools] [--jobs-checked-every-ms N] [--push-relay URL] \
[--schedules-checked-every-ms N] [--heartbeats-checked-every-ms N] \
[--janitors-checked-every-ms N] [--goals-kept-every-ms N] \
[--accounts none|follow|select] [--seats-tool PROGRAM] [--logins-checked-every-ms N] \
[--helpers-checked-every-ms N] [--helper-idle-after-ms N] [--helper-archive-grace-ms N] \
[--whisper-python PATH] [--whisper-worker PATH] [--whisper-model NAME] [--whisper-source NAME|DIR] \
[--whisper-compute KIND] [--speech-language CODE] [--speech-keys FILE] [--transcription-engine NAME]
[--push-keys FILE]
[--static DIR] [--relay-config FILE]
The token the apps must present is read from CLARP_HOST_TOKEN, and this Host's credential at
the push relay, when it has one, from CLARP_PUSH_CREDENTIAL. The key of an engine that listens
over the network is looked up in the file --speech-keys names, then in ELEVEN_API_KEY. A ring is
pushed to a phone as an incoming call only where --push-keys names the file that has the owner's
key at the push service ([apns] key_path, key_id, team_id).
CLARP_HEARTBEATS_DISABLED=1 turns the heartbeat of every agent off.";

/// The home of whoever runs this Host.
pub fn home() -> PathBuf {
    std::env::var_os("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from)
}

/// A path as a person types it on a phone, as a path on this machine: `~` is the home of
/// whoever runs this Host, nothing is that home too, and a path under `/home` that is not
/// there is tried under that home (`/home/projects/site` for `~/projects/site`: the name of
/// the account is what a phone's user leaves out). Written without what does not count: a
/// slash at its end, two slashes in a row, a part that is only a dot.
pub fn typed_path(asked: &str, home: &Path) -> PathBuf {
    let asked = asked.trim();
    let named = match asked.strip_prefix('~') {
        _ if asked.is_empty() => home.to_path_buf(),
        Some("") => home.to_path_buf(),
        Some(under) if under.starts_with('/') => home.join(under.trim_start_matches('/')),
        _ => PathBuf::from(asked),
    };
    let named: PathBuf = named.components().collect();
    if named.exists() || !named.is_absolute() {
        return named;
    }
    match named.strip_prefix("/home") {
        Ok(under) if home.join(under).exists() => home.join(under),
        _ => named,
    }
}

/// The directories a path that is being typed can go on with, twenty at most: for a path
/// that ends in a slash the directories in it, for any other the directories beside it
/// whose names begin as its last part does, whatever the case of the letters. What begins
/// with a dot is left out. Answers where it looked, and what it found there, by name.
///
/// Only a path from the root is looked into: `./` and `../` are not this Host's own
/// directory to show, as on the Python Host. Where nothing can be read, nothing is found.
pub fn directories(asked: &str, home: &Path) -> (String, Vec<String>) {
    // Another user's home (`~name`) is that user's, where this machine knows the user. A
    // name it does not know is answered where it was asked, with nothing found, as on the
    // Python Host: what is typed on a phone is not refused.
    let trimmed = asked.trim();
    let another = trimmed.strip_prefix('~');
    let another = another.filter(|rest| !rest.is_empty() && !rest.starts_with('/'));
    let theirs = match another {
        Some(rest) => {
            let (name, under) = rest
                .split_once('/')
                .map_or((rest, None), |(name, under)| (name, Some(under)));
            let known = nix::unistd::User::from_name(name).ok().flatten();
            let Some(user) = known else {
                return (trimmed.to_string(), Vec::new());
            };
            let home = user.dir.display().to_string();
            Some(under.map_or(home.clone(), |under| format!("{home}/{under}")))
        }
        None => None,
    };
    let asked = theirs.as_deref().unwrap_or(asked);
    let typed = typed_path(asked, home);
    let ends_in_slash = asked.trim().ends_with('/') && typed.is_absolute();
    let inside = ends_in_slash || typed == Path::new("/");
    // Beside it: where its last name is, and how that name begins. Read off the letters of
    // the path, as the Python Host reads them: a path that is one name is completed among
    // what is under the root, and a last name that is a dot begins nothing that is shown.
    let (base, begins) = if inside {
        (typed, String::new())
    } else {
        let written = typed.display().to_string();
        let (before, last) = written.rsplit_once('/').unwrap_or(("", &written));
        let beside = if before.is_empty() { "/" } else { before };
        (PathBuf::from(beside), last.to_lowercase())
    };
    let mut names: Vec<String> = std::fs::read_dir(&base)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| !name.starts_with('.') && name.to_lowercase().starts_with(&begins))
        .collect();
    names.sort();
    let found = names.iter().map(|name| base.join(name));
    let found = found.filter(|path| path.is_dir()).take(20);
    let found = found.map(|path| path.display().to_string()).collect();
    (base.display().to_string(), found)
}

/// The directory an agent works in, by what was asked for it: `~` is the home of whoever
/// runs this Host, nothing is the Host's own directory for agents, and so is a directory
/// that is not there. Nothing is refused, as on the Python Host: an app sends `~` when its
/// user named no directory, and a phone's keyboard misspells a path.
pub fn working_dir(asked: &str, home: &Path, otherwise: &Path) -> PathBuf {
    let asked = asked.trim();
    let named = match asked.strip_prefix('~') {
        _ if asked.is_empty() => otherwise.to_path_buf(),
        Some("") => home.to_path_buf(),
        Some(under) if under.starts_with('/') => home.join(under.trim_start_matches('/')),
        _ => PathBuf::from(asked),
    };
    if named.is_dir() {
        named
    } else {
        otherwise.to_path_buf()
    }
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
        let mut seats_tool = String::new();
        let mut logins_checked_every_ms = 5_000;
        let mut jobs_checked_every_ms = 30_000;
        let mut schedules_checked_every_ms = 15_000;
        let mut heartbeats_checked_every_ms = 60_000;
        let mut janitors_checked_every_ms = 2_000;
        let mut goals_kept_every_ms = 15_000;
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
        let mut push_keys = None;
        let mut static_dir = None;
        let mut relay_config = None;
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
                        .ok_or(format!("--accounts {named}: none, follow or select"))?;
                }
                "--seats-tool" => seats_tool = value("a program")?,
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
                "--schedules-checked-every-ms" => {
                    schedules_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--schedules-checked-every-ms needs a number".to_string())?;
                }
                "--heartbeats-checked-every-ms" => {
                    heartbeats_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--heartbeats-checked-every-ms needs a number".to_string())?;
                }
                "--janitors-checked-every-ms" => {
                    janitors_checked_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--janitors-checked-every-ms needs a number".to_string())?;
                }
                "--goals-kept-every-ms" => {
                    goals_kept_every_ms = value("a number of milliseconds")?
                        .parse()
                        .map_err(|_| "--goals-kept-every-ms needs a number".to_string())?;
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
                "--push-keys" => push_keys = Some(PathBuf::from(value("a file")?)),
                "--static" => static_dir = Some(PathBuf::from(value("a directory")?)),
                "--relay-config" => relay_config = Some(PathBuf::from(value("a file")?)),
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
        let nova_endpoint = stand_in("CLARP_NOVA_ENDPOINT")?;
        // The engine of a stream is reached by a socket: its stand-in is one on this machine.
        let flux_endpoint = std::env::var("CLARP_FLUX_ENDPOINT").unwrap_or_default();
        let local = ["ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:"];
        if !flux_endpoint.is_empty() && !local.iter().any(|begin| flux_endpoint.starts_with(begin))
        {
            let not_here = format!("CLARP_FLUX_ENDPOINT {flux_endpoint}: a socket on this machine");
            return Err(not_here);
        }
        let live_endpoint = std::env::var("CLARP_LIVE_ENDPOINT").unwrap_or_default();
        if !live_endpoint.is_empty() && !local.iter().any(|begin| live_endpoint.starts_with(begin))
        {
            let not_here = format!("CLARP_LIVE_ENDPOINT {live_endpoint}: a socket on this machine");
            return Err(not_here);
        }
        let router_endpoint = stand_in("CLARP_ROUTER_ENDPOINT")?;
        let cartesia_endpoint = stand_in("CLARP_CARTESIA_ENDPOINT")?;
        let apns_endpoint = stand_in("CLARP_APNS_ENDPOINT")?;
        let fcm_endpoint = std::env::var("CLARP_FCM_ENDPOINT")
            .ok()
            .map(|address| {
                // A single literal loopback socket replaces BOTH credential exchange and messaging.
                address
                    .strip_prefix("http://")
                    .and_then(|host| host.parse::<std::net::SocketAddr>().ok())
                    .filter(|at| at.ip().is_loopback() && at.port() != 0)
                    .map(|at| format!("http://{at}"))
                    .ok_or_else(|| {
                        "CLARP_FCM_ENDPOINT needs a loopback HTTP socket address".to_string()
                    })
            })
            .transpose()?;
        let gemini_endpoint = stand_in("CLARP_GEMINI_ENDPOINT")?;
        let google_token_endpoint = stand_in("CLARP_GOOGLE_TOKEN_ENDPOINT")?;
        let google_speech_endpoint = stand_in("CLARP_GOOGLE_SPEECH_ENDPOINT")?;
        // A token of the owner's real account is never handed to a stand-in, and a token a
        // stand-in made up is never sent to Google: both are stood in for, or neither.
        if google_token_endpoint.is_empty() != google_speech_endpoint.is_empty() {
            let both =
                "CLARP_GOOGLE_TOKEN_ENDPOINT and CLARP_GOOGLE_SPEECH_ENDPOINT are given together";
            return Err(both.to_string());
        }
        let chirp_decoder = std::env::var("CLARP_CHIRP_DECODER").unwrap_or_default();
        // A stand-in for the relay is on this machine too: the key a Host dials with goes
        // over TLS, or stays here.
        let relay_stand_in = std::env::var("CLARP_RELAY_DIAL").unwrap_or_default();
        let here = ["ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:"];
        if !relay_stand_in.is_empty() && !here.iter().any(|begin| relay_stand_in.starts_with(begin))
        {
            let only = "an address on this machine (ws://127.0.0.1:PORT)";
            return Err(format!("CLARP_RELAY_DIAL {relay_stand_in}: {only}"));
        }
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
            seats_tool,
            logins_checked_every_ms,
            jobs_checked_every_ms,
            schedules_checked_every_ms,
            heartbeats_checked_every_ms,
            janitors_checked_every_ms,
            goals_kept_every_ms,
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
            push_keys,
            static_dir,
            transcription_engine,
            scribe_endpoint,
            nova_endpoint,
            flux_endpoint,
            live_endpoint,
            router_endpoint,
            cartesia_endpoint,
            apns_endpoint,
            fcm_endpoint,
            gemini_endpoint,
            google_token_endpoint,
            google_speech_endpoint,
            chirp_decoder,
            relay_config,
            relay_stand_in,
            heartbeats_disabled: {
                let said = std::env::var("CLARP_HEARTBEATS_DISABLED").unwrap_or_default();
                matches!(said.to_lowercase().as_str(), "1" | "true" | "yes")
            },
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
    fn a_path_that_is_being_typed_is_answered_with_the_directories_it_can_go_on_with() {
        let root = std::env::temp_dir().join(format!("clarp-dirs-{}", std::process::id()));
        let home = root.join("home");
        for name in [
            "projects/site",
            "projects/shop",
            "Pictures",
            ".hidden",
            "plans",
        ] {
            std::fs::create_dir_all(home.join(name)).unwrap();
        }
        std::fs::write(home.join("projects/notes.txt"), b"x").unwrap();
        std::fs::write(home.join("paper"), b"x").unwrap();
        let at = |path: &str| match path {
            "" => root.display().to_string(),
            path => root.join(path).display().to_string(),
        };
        let asked = |asked: &str| directories(asked, &home);
        // What the Python Host answers (run on the same tree with its own function).
        assert_eq!(asked(""), (at(""), vec![at("home")]));
        assert_eq!(asked("~"), (at(""), vec![at("home")]));
        let with_p = vec![at("home/Pictures"), at("home/plans"), at("home/projects")];
        assert_eq!(asked("~/p"), (at("home"), with_p));
        assert_eq!(asked("~/projects"), (at("home"), vec![at("home/projects")]));
        let in_projects = vec![at("home/projects/shop"), at("home/projects/site")];
        assert_eq!(
            asked(" ~/projects/s "),
            (at("home/projects"), in_projects.clone())
        );
        assert_eq!(
            asked("~/nothing/x"),
            (at("home/nothing"), Vec::<String>::new())
        );
        // The root is looked into itself: directories right under it, whichever a machine has.
        let (base, under_root) = asked("/");
        assert_eq!(base, "/");
        assert!(
            !under_root.is_empty() && under_root.len() <= 20,
            "{under_root:?}"
        );
        for path in &under_root {
            assert!(
                path.starts_with('/') && path.matches('/').count() == 1,
                "{path}"
            );
        }
        // A path that ends in a slash asks what is in the directory. (Until 16 Nov 2026 the
        // Python Host answered it with the directory itself: reference defect R22, fixed.)
        assert_eq!(
            asked("~/projects/"),
            (at("home/projects"), in_projects.clone())
        );
        assert_eq!(
            asked(&format!("{}/", at("home/projects"))),
            (at("home/projects"), in_projects)
        );
        let in_home = vec![at("home/Pictures"), at("home/plans"), at("home/projects")];
        assert_eq!(asked("~/"), (at("home"), in_home));
        // Only a path from the root is looked into: one that is not is completed by its last
        // name, among what is under the root, as on the Python Host.
        assert_eq!(asked("relative/"), ("/".to_string(), Vec::<String>::new()));
        assert_eq!(asked("./"), ("/".to_string(), Vec::<String>::new()));
        // The name of the account is what a phone's user leaves out: tried under the home.
        assert_eq!(
            typed_path("/home/projects/site", &home),
            home.join("projects/site")
        );
        assert_eq!(
            typed_path("/home/nothing/there", &home),
            PathBuf::from("/home/nothing/there")
        );
        assert_eq!(
            typed_path("relative/path/", &home),
            PathBuf::from("relative/path")
        );
        // Another user's home: looked into where this machine knows the user, and answered
        // with nothing where it does not (the Python Host since 16 Nov 2026).
        let nobody = "~nobody-of-that-name-here";
        assert_eq!(
            asked(&format!(" {nobody}/x ")),
            (format!("{nobody}/x"), Vec::<String>::new())
        );
        assert_eq!(asked(nobody), (nobody.to_string(), Vec::<String>::new()));
        assert_eq!(asked("~root").0, "/");
        assert!(asked("~root").1.contains(&"/root".to_string()));
        assert_eq!(asked("~root/").0, "/root");
        // No more than twenty.
        for count in 0..25 {
            std::fs::create_dir_all(home.join(format!("many/d{count:02}"))).unwrap();
        }
        let many = asked("~/many/");
        assert_eq!(many.1.len(), 20);
        assert_eq!(many.1.first(), Some(&at("home/many/d00")));
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn an_agent_works_where_it_was_asked_to_or_in_the_hosts_own_directory() {
        let root = std::env::temp_dir().join(format!("clarp-working-dir-{}", std::process::id()));
        let (home, own) = (root.join("home"), root.join("agents"));
        std::fs::create_dir_all(home.join("projects/site")).unwrap();
        std::fs::create_dir_all(&own).unwrap();
        let at = |asked: &str| working_dir(asked, &home, &own);
        assert_eq!(at("~"), home);
        assert_eq!(at(" ~/projects/site "), home.join("projects/site"));
        assert_eq!(at("~/projects//site"), home.join("projects//site"));
        assert_eq!(
            at(home.join("projects").to_str().unwrap()),
            home.join("projects")
        );
        // Nothing asked for, a directory that is not there, another user's home, a file.
        assert_eq!(at(""), own);
        assert_eq!(at("   "), own);
        assert_eq!(at("~/nothing-of-the-kind"), own);
        assert_eq!(at("/nothing/of/the/kind"), own);
        assert_eq!(at("~somebody"), own);
        std::fs::write(home.join("a-file"), b"x").unwrap();
        assert_eq!(at("~/a-file"), own);
        std::fs::remove_dir_all(&root).unwrap();
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
        // And it dials no relay for the phones until it is given the file that says so.
        assert_eq!(config.relay_config, None);
        assert_eq!(
            parse(&["--relay-config", "/c/config.toml"])
                .unwrap()
                .relay_config,
            Some(PathBuf::from("/c/config.toml"))
        );
    }
}
