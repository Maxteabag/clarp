//! What a Host is told when it starts.
//!
//! Every part that has more than one implementation is chosen here by name: the engine that
//! stores the logs, the compressor, the way shims are started. Changing one is a restart with a
//! different word, not a change of code.

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
}

pub const USAGE: &str = "usage: clarp-host [--data DIR] [--run DIR] [--listen ADDR:PORT] \
[--engine sqlite|memory] [--codec zstd|none] [--launcher direct|systemd] [--shim PATH] \
[--name NAME] [--cwd DIR] [--keep-spools]
The token the apps must present is read from CLARP_HOST_TOKEN.";

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
        let mut launcher = "direct".to_string();
        let mut shim = None;
        let mut name = "Clarp Host (Rust)".to_string();
        let mut default_cwd = home();
        let mut keep_spools = false;
        while let Some(arg) = args.next() {
            let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
            match arg.as_str() {
                "--data" => data_dir = PathBuf::from(value("a directory")?),
                "--run" => run_dir = Some(PathBuf::from(value("a directory")?)),
                "--listen" => listen = value("an address")?,
                "--engine" => engine = value("a name")?,
                "--codec" => codec = value("a name")?,
                "--launcher" => launcher = value("a name")?,
                "--shim" => shim = Some(PathBuf::from(value("a path")?)),
                "--name" => name = value("a name")?,
                "--cwd" => default_cwd = PathBuf::from(value("a directory")?),
                "--keep-spools" => keep_spools = true,
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
        Ok(Self {
            data_dir,
            run_dir,
            listen: listen
                .parse()
                .map_err(|error| format!("--listen {listen}: {error}"))?,
            engine,
            codec,
            launcher,
            shim,
            auth_token: std::env::var("CLARP_HOST_TOKEN").unwrap_or_default(),
            name,
            default_cwd,
            keep_spools,
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
        assert!(config.keep_spools);
        assert!(parse(&["--what"]).is_err());
        assert!(parse(&["--data"]).is_err());
        assert!(parse(&["--listen", "nowhere"]).is_err());
    }
}
