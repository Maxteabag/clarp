//! A lane's tool, tied to the lane's process (issue #772). A lane starts its tool through
//! `clarp-shim tool-child <lane pid> -- <program> <args...>`: this process asks the kernel to
//! end it with SIGKILL when its parent ends (`PR_SET_PDEATHSIG`), makes sure that parent is
//! still the lane, and then becomes the tool in place (`execvp`), so the tool keeps the process
//! id, the group, the pipes, the folder and the environment the lane started it with, and the
//! death signal with them. A tool that hangs, and would not read the end of its input, so ends
//! with its lane however the lane ends: killed by the Host, or failed by itself. Without it, a
//! hung tool outlived its lane after the lane's lock on the conversation was let go, and wrote
//! beside the next lane or turn.
//!
//! The death signal is set here rather than between fork and exec in the lane, because that
//! would need `pre_exec`, which is `unsafe`, and the workspace forbids it. It fires when the
//! thread that started the child ends: both lanes start their tool on their main thread, which
//! lives as long as the lane's process.

use std::ffi::CString;
use std::process::ExitCode;

use nix::sys::signal::Signal;

/// The word that selects this mode.
pub(crate) const MODE: &str = "tool-child";

/// The command a lane starts its tool `program` with, through this shim. `/proc/self/exe` names
/// the shim's own image in the child, also after its file was replaced by an update.
pub(crate) fn command(program: impl AsRef<std::ffi::OsStr>) -> std::process::Command {
    let mut command = std::process::Command::new("/proc/self/exe");
    command
        .arg(MODE)
        .arg(std::process::id().to_string())
        .arg("--")
        .arg(program);
    command
}

/// Runs this mode with what follows its word: the lane's process id, `--`, the program and its
/// arguments. Returns only when the tool could not be started.
pub(crate) fn run(mut args: impl Iterator<Item = String>) -> ExitCode {
    let lane = args.next().and_then(|pid| pid.parse::<i32>().ok());
    let (Some(lane), Some("--")) = (lane, args.next().as_deref()) else {
        eprintln!("usage: clarp-shim {MODE} LANE_PID -- PROGRAM [ARGS...]");
        return ExitCode::from(2);
    };
    let Ok(argv) = args.map(CString::new).collect::<Result<Vec<_>, _>>() else {
        eprintln!("clarp-shim {MODE}: an argument holds a NUL byte");
        return ExitCode::from(2);
    };
    let Some(program) = argv.first() else {
        eprintln!("clarp-shim {MODE}: no program");
        return ExitCode::from(2);
    };
    if let Err(error) = nix::sys::prctl::set_pdeathsig(Signal::SIGKILL) {
        eprintln!("clarp-shim {MODE}: the death signal could not be set: {error}");
        return ExitCode::from(126);
    }
    // The lane may have ended before the death signal was set: then nothing would end this
    // process with it, so it does not become the tool.
    if nix::unistd::getppid().as_raw() != lane {
        eprintln!("clarp-shim {MODE}: the lane {lane} has already ended");
        return ExitCode::from(1);
    }
    let error = match nix::unistd::execvp(program, &argv) {
        Ok(never) => match never {},
        Err(error) => error,
    };
    eprintln!("clarp-shim {MODE}: {program:?} could not be started: {error}");
    ExitCode::from(127)
}
