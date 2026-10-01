//! Command-line launch options (C++ `main.cpp` `addLaunchOptions`): start
//! an agent (anonymously, as a contact, with a backend, directory, model or
//! effort), open without one, or manage preview versions.

/// Set by a preview relaunch for the new window only; processes a window
/// starts must not inherit them.
pub const RESTORE_VARIABLES: &[&str] = &["CLARP_RESTORE_DESKTOP", "CLARP_RESTORE_SESSION"];

pub const BACKENDS: &[&str] = &["claude", "codex", "grok", "agy", "opencode", "deepseek"];

pub const HELP: &str = "Usage: clarp-desktop [options]
Clarp desktop and agent launcher

Options:
  -h, --help               Displays help on commandline options.
  -v, --version            Displays version information.
  --anonymous              Start anonymously, overriding Settings
  --contact                Start with an available contact, overriding Settings
  --new-agent              Start an agent; prompt for backend if omitted
  --no-new-agent           Open the desktop without starting an agent, overriding Settings
  --backend <backend>      Start with claude, codex, grok, agy, opencode, or deepseek (implies --new-agent)
  --cwd <directory>        Workspace directory; skip directory selection
  --model <model>          Use this backend model ID
  --effort <effort>        Use this model reasoning effort
  --preview-versions       Manage saved preview versions
";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LaunchOptions {
    pub help: bool,
    pub version: bool,
    pub anonymous: bool,
    pub contact: bool,
    pub new_agent: bool,
    pub no_new_agent: bool,
    pub backend: String,
    /// `--backend` was given, even empty (which is invalid).
    pub backend_set: bool,
    pub cwd: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub preview_versions: bool,
}

impl LaunchOptions {
    /// 1 anonymous, 0 contact, -1 as Settings say.
    pub fn anonymous_mode(&self) -> i32 {
        if self.anonymous {
            1
        } else if self.contact {
            0
        } else {
            -1
        }
    }

    /// The flags ask for an agent explicitly.
    pub fn explicit_agent_launch(&self) -> bool {
        self.cwd.is_some() || self.anonymous || self.contact || self.new_agent || self.backend_set || self.model.is_some() || self.effort.is_some()
    }

    /// Start an agent once the window opens: asked for, or Settings say so
    /// (not in screenshot runs); never when restoring or managing versions.
    pub fn launch_on_startup(&self, restoring: bool, new_agent_setting: bool, screenshot: bool) -> bool {
        !restoring && !self.preview_versions && !self.no_new_agent && (self.explicit_agent_launch() || (new_agent_setting && !screenshot))
    }

    /// Only a launch that names a backend starts on its own; it pauses
    /// fleet loading so the new agent is not slowed by it.
    pub fn auto_start(&self, launch_on_startup: bool) -> bool {
        launch_on_startup && !self.backend.is_empty()
    }

    /// `--no-new-agent`: the desktop waits for the reader to pick a chat.
    pub fn empty_startup(&self, restoring: bool) -> bool {
        !restoring && !self.preview_versions && self.no_new_agent
    }
}

/// Parses `arguments` (without the program name). Unknown options and
/// probe-only `--probe-*` arguments are ignored, as Qt's parser is told to.
pub fn parse(arguments: &[String]) -> Result<LaunchOptions, String> {
    let mut options = LaunchOptions::default();
    let mut rest = arguments.iter();
    while let Some(argument) = rest.next() {
        let (name, inline) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_owned())),
            _ => (argument.as_str(), None),
        };
        let mut value = |label: &str| -> Result<String, String> {
            match inline.clone().or_else(|| rest.next().cloned()) {
                Some(value) => Ok(value),
                None => Err(format!("Missing value after '{label}'.")),
            }
        };
        match name {
            "-h" | "--help" => options.help = true,
            "-v" | "--version" => options.version = true,
            "--anonymous" => options.anonymous = true,
            "--contact" => options.contact = true,
            "--new-agent" => options.new_agent = true,
            "--no-new-agent" => options.no_new_agent = true,
            "--preview-versions" => options.preview_versions = true,
            "--backend" => {
                options.backend = value("--backend")?.trim().to_lowercase();
                options.backend_set = true;
            }
            "--cwd" => options.cwd = Some(value("--cwd")?),
            "--model" => options.model = Some(value("--model")?),
            "--effort" => options.effort = Some(value("--effort")?),
            _ => {}
        }
    }
    let conflicting = (options.anonymous && options.contact)
        || (options.explicit_agent_launch() && options.no_new_agent)
        || (options.backend_set && !BACKENDS.contains(&options.backend.as_str()));
    if conflicting {
        return Err("Invalid backend or conflicting agent launch flags. See --help.".into());
    }
    Ok(options)
}
