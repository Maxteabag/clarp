//! The production download port starts one child through the Host's child launcher.
use clarp_ports::model_download::{DownloadRequest, DownloadRun, ModelDownloader};
use nix::sys::signal::{Signal, killpg};
use nix::sys::wait::{Id, WaitPidFlag, WaitStatus, waitid};
use nix::unistd::Pid;
use std::io;
use std::os::unix::process::CommandExt as _;
use std::path::PathBuf;
use std::process::{Child, Stdio};

pub(super) struct SpeechDownloader {
    pub python: PathBuf,
    pub script: PathBuf,
}

impl ModelDownloader for SpeechDownloader {
    fn start(&self, request: DownloadRequest) -> io::Result<Box<dyn DownloadRun>> {
        if self.python.as_os_str().is_empty() {
            return Err(io::Error::other("speech environment unavailable"));
        }
        // Python rejects a directory on stdin. Move it to fd 3 before exec; the
        // script is constant and the interpreter, script and arguments stay argv.
        let child = clarp_child::command("/bin/sh")
            .args([
                "-c",
                r#"exec 3<&0 </dev/null; exec "$@""#,
                "transcription-download",
            ])
            .arg(&self.python)
            .arg(&self.script)
            .args([
                "--repository",
                request.repository,
                "--revision",
                request.revision,
            ])
            .arg("--maximum")
            .arg(request.maximum_bytes.to_string())
            .arg("--parent")
            .arg(std::process::id().to_string())
            .env("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
            .stdin(request.staging)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()?;
        Ok(Box::new(Process(Some(child))))
    }
}

struct Process(Option<Child>);

impl Process {
    fn end(&mut self) -> io::Result<std::process::ExitStatus> {
        let child = self
            .0
            .as_mut()
            .ok_or_else(|| io::Error::other("download already ended"))?;
        // No other code reaps this child. Its PID still identifies the process group
        // we created, including when the leader has exited and waitid observed it.
        match killpg(Pid::from_raw(child.id() as i32), Signal::SIGKILL) {
            Ok(()) | Err(nix::errno::Errno::ESRCH) => {}
            Err(e) => return Err(e.into()),
        }
        let status = child.wait()?;
        self.0.take();
        Ok(status)
    }
}

impl DownloadRun for Process {
    fn try_complete(&mut self) -> io::Result<Option<()>> {
        let Some(child) = self.0.as_ref() else {
            return Err(io::Error::other("download already ended"));
        };
        let id = Id::Pid(Pid::from_raw(child.id() as i32));
        match waitid(
            id,
            WaitPidFlag::WEXITED | WaitPidFlag::WNOWAIT | WaitPidFlag::WNOHANG,
        ) {
            Ok(WaitStatus::StillAlive) | Err(nix::errno::Errno::EINTR) => Ok(None),
            Ok(_) => {
                let status = self.end()?;
                if status.success() {
                    Ok(Some(()))
                } else {
                    Err(io::Error::other(format!(
                        "install_exit_{}",
                        status.code().unwrap_or(1)
                    )))
                }
            }
            Err(e) => Err(e.into()),
        }
    }

    fn cancel(&mut self) -> io::Result<()> {
        if self.0.is_some() {
            self.end()?;
        }
        Ok(())
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.cancel();
    }
}
