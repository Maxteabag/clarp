//! QObject over `clarp_core::preview::PreviewVersions`: runs the preview
//! helper off the GUI thread, refreshes its catalog every 15 s, and relaunches
//! into the same Host and conversation after a version is pinned.

use std::pin::Pin;
use std::process::{Command, Stdio};
use std::time::Duration;

use clarp_core::preview::{Finished, Launch, PREVIEW_PYTHON, PreviewVersions, REFRESH_MS, relaunch_environment};
use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::{QJsonObject, QString};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qjsonobject.h");
        type QJsonObject = cxx_qt_lib::QJsonObject;
        include!(<QtCore/QCoreApplication>);
        type QCoreApplication;
        /// Ends the event loop so destructors (draft flushes) run, like the
        /// C++ `QCoreApplication::quit()` after a relaunch starts.
        #[Self = "QCoreApplication"]
        fn quit();
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(bool, restart_allowed, cxx_name = "restartAllowed", READ = restart_allowed_value, WRITE = set_restart_allowed, NOTIFY = changed)]
        #[qproperty(QString, selected_session, cxx_name = "selectedSession", READ = selected_session_value, WRITE = set_selected_session, NOTIFY = changed)]
        #[qproperty(QString, selected_host, cxx_name = "selectedHost", READ = selected_host_value, WRITE = set_selected_host, NOTIFY = changed)]
        #[qproperty(bool, enabled, READ = enabled_value, CONSTANT)]
        #[qproperty(bool, busy, READ = busy_value, NOTIFY = changed)]
        #[qproperty(QString, running_hash, cxx_name = "runningHash", READ = running_hash_value, CONSTANT)]
        #[qproperty(QJsonObject, catalog, READ = catalog_value, NOTIFY = changed)]
        #[qproperty(QString, error, READ = error_value, NOTIFY = changed)]
        #[qproperty(QString, notice, READ = notice_value, NOTIFY = changed)]
        type PreviewVersions = super::PreviewVersionsRust;
    }

    unsafe extern "RustQt" {
        fn restart_allowed_value(self: &PreviewVersions) -> bool;
        #[cxx_name = "setRestartAllowed"]
        fn set_restart_allowed(self: Pin<&mut PreviewVersions>, value: bool);
        fn selected_session_value(self: &PreviewVersions) -> QString;
        #[cxx_name = "setSelectedSession"]
        fn set_selected_session(self: Pin<&mut PreviewVersions>, value: &QString);
        fn selected_host_value(self: &PreviewVersions) -> QString;
        #[cxx_name = "setSelectedHost"]
        fn set_selected_host(self: Pin<&mut PreviewVersions>, value: &QString);
        fn enabled_value(self: &PreviewVersions) -> bool;
        fn busy_value(self: &PreviewVersions) -> bool;
        fn running_hash_value(self: &PreviewVersions) -> QString;
        fn catalog_value(self: &PreviewVersions) -> QJsonObject;
        fn error_value(self: &PreviewVersions) -> QString;
        fn notice_value(self: &PreviewVersions) -> QString;

        #[qsignal]
        fn changed(self: Pin<&mut PreviewVersions>);

        #[qinvokable]
        #[cxx_name = "restartContext"]
        fn restart_context(self: &PreviewVersions) -> QJsonObject;
        #[qinvokable]
        fn refresh(self: Pin<&mut PreviewVersions>);
        #[qinvokable]
        #[cxx_name = "selectVersion"]
        fn select_version(self: Pin<&mut PreviewVersions>, hash: &QString);
    }

    impl cxx_qt::Threading for PreviewVersions {}
    impl cxx_qt::Initialize for PreviewVersions {}
}

#[derive(Default)]
pub struct PreviewVersionsRust {
    state: PreviewVersions,
    busy: bool,
}

fn launch() -> Launch {
    let home = std::env::var("HOME").unwrap_or_default();
    let manager = std::env::args().any(|a| a == "--preview-versions");
    let helper = clarp_core::preview::helper_path(&home);
    let executable_hash = clarp_core::preview::file_sha256(std::path::Path::new("/proc/self/exe")).unwrap_or_default();
    Launch {
        helper_exists: std::path::Path::new(&helper).exists(),
        home,
        instance_name: std::env::var("CLARP_INSTANCE_NAME").unwrap_or_default(),
        manager,
        screenshot: std::env::var_os("CLARP_SCREENSHOT_PATH").is_some(),
        screenshot_scenario: std::env::var("CLARP_SCREENSHOT_SCENARIO").unwrap_or_default(),
        executable_hash,
    }
}

impl cxx_qt::Initialize for qobject::PreviewVersions {
    fn initialize(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().state = PreviewVersions::new(&launch());
        if self.state.polls() {
            self.as_mut().schedule_refresh(Duration::ZERO);
        }
    }
}

impl qobject::PreviewVersions {
    fn restart_allowed_value(&self) -> bool {
        self.state.restart_allowed
    }
    fn set_restart_allowed(mut self: Pin<&mut Self>, value: bool) {
        if self.state.restart_allowed != value {
            self.as_mut().rust_mut().state.restart_allowed = value;
            self.changed();
        }
    }
    fn selected_session_value(&self) -> QString {
        QString::from(self.state.selected_session.as_str())
    }
    fn set_selected_session(mut self: Pin<&mut Self>, value: &QString) {
        let value = value.to_string();
        if self.state.selected_session != value {
            self.as_mut().rust_mut().state.selected_session = value;
            self.changed();
        }
    }
    fn selected_host_value(&self) -> QString {
        QString::from(self.state.selected_host.as_str())
    }
    fn set_selected_host(mut self: Pin<&mut Self>, value: &QString) {
        let value = value.to_string();
        if self.state.selected_host != value {
            self.as_mut().rust_mut().state.selected_host = value;
            self.changed();
        }
    }
    fn enabled_value(&self) -> bool {
        self.state.enabled()
    }
    fn busy_value(&self) -> bool {
        self.busy
    }
    fn running_hash_value(&self) -> QString {
        QString::from(self.state.running_hash())
    }
    fn catalog_value(&self) -> QJsonObject {
        crate::qjson::to_qjson(self.state.catalog()).to_object()
    }
    fn error_value(&self) -> QString {
        QString::from(self.state.error())
    }
    fn notice_value(&self) -> QString {
        QString::from(self.state.notice())
    }

    fn restart_context(&self) -> QJsonObject {
        crate::qjson::to_qjson(&self.state.restart_context(std::process::id())).to_object()
    }

    fn refresh(mut self: Pin<&mut Self>) {
        if let Some(arguments) = self.state.refresh(self.busy) {
            self.as_mut().run_helper(arguments);
        }
    }

    fn select_version(mut self: Pin<&mut Self>, hash: &QString) {
        let busy = self.busy;
        let arguments = self.as_mut().rust_mut().state.select_version(&hash.to_string(), busy);
        match arguments {
            Some(arguments) => self.run_helper(arguments),
            None => self.changed(),
        }
    }

    fn schedule_refresh(self: Pin<&mut Self>, delay: Duration) {
        let qt = self.qt_thread();
        crate::runtime::after(delay, move || {
            let queued = qt.queue(|mut versions| {
                versions.as_mut().refresh();
                versions.schedule_refresh(Duration::from_millis(REFRESH_MS));
            });
            if queued.is_err() {
                eprintln!("PreviewVersions: refresh timer stopped; the object is gone");
            }
        });
    }

    fn run_helper(mut self: Pin<&mut Self>, arguments: Vec<String>) {
        self.as_mut().rust_mut().busy = true;
        self.as_mut().changed();
        let qt = self.qt_thread();
        crate::runtime::handle().spawn_blocking(move || {
            let output = Command::new(PREVIEW_PYTHON).args(&arguments).stdin(Stdio::null()).stderr(Stdio::inherit()).output();
            let queued = qt.queue(move |versions| versions.helper_finished(output));
            if queued.is_err() {
                eprintln!("PreviewVersions: dropped a helper result; the object is gone");
            }
        });
    }

    fn helper_finished(mut self: Pin<&mut Self>, output: std::io::Result<std::process::Output>) {
        self.as_mut().rust_mut().busy = false;
        let next = match output {
            Err(error) => {
                eprintln!("PreviewVersions: cannot run {PREVIEW_PYTHON}: {error}");
                self.as_mut().rust_mut().state.start_failed();
                Finished::Nothing
            }
            Ok(output) => self.as_mut().rust_mut().state.finished(output.status.success(), &output.stdout),
        };
        match next {
            Finished::Nothing => {}
            Finished::Refresh => {
                self.as_mut().changed();
                self.as_mut().refresh();
                return;
            }
            Finished::Relaunch => self.as_mut().relaunch(),
        }
        self.changed();
    }

    fn relaunch(mut self: Pin<&mut Self>) {
        let context = self.state.restart_context(std::process::id());
        let host = context["host"].as_str().unwrap_or_default().to_owned();
        let session = context["session"].as_str().unwrap_or_default().to_owned();
        let arguments: Vec<String> = clarp_core::preview::relaunch_arguments(self.state.helper(), std::process::id());
        let spawned = Command::new(PREVIEW_PYTHON)
            .args(&arguments)
            .envs(relaunch_environment(&host, &session))
            .stdin(Stdio::null())
            .spawn();
        match spawned {
            Ok(_) => qobject::QCoreApplication::quit(),
            Err(error) => {
                eprintln!("PreviewVersions: relaunch failed: {error}");
                self.as_mut().rust_mut().state.relaunch_failed();
            }
        }
    }
}
