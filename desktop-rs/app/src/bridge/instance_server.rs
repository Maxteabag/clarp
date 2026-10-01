//! InstanceServer: opens a window here for a second launch of the same
//! Clarp (C++ `InstanceServer`; see `clarp_core::instance`). main forwards
//! a launch to a running instance before Qt starts; when none answered, the
//! root document hands this the socket to listen on and opens a window for
//! each launch it receives.

use std::pin::Pin;

use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::QString;

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(QString, socket_path, cxx_name = "socketPath", READ, WRITE = set_socket_path, NOTIFY = socket_path_changed)]
        #[qproperty(bool, listening, READ, NOTIFY = listening_changed)]
        type InstanceServer = super::InstanceServerRust;
    }

    unsafe extern "RustQt" {
        #[cxx_name = "setSocketPath"]
        fn set_socket_path(self: Pin<&mut InstanceServer>, path: QString);
        #[qsignal]
        #[cxx_name = "socketPathChanged"]
        fn socket_path_changed(self: Pin<&mut InstanceServer>);
        #[qsignal]
        #[cxx_name = "listeningChanged"]
        fn listening_changed(self: Pin<&mut InstanceServer>);
        /// A forwarded launch, resolved like the first one (C++ main's
        /// `windowRequested` handler): start an agent, or open with the
        /// chat list, and with what.
        #[qsignal]
        #[cxx_name = "windowRequested"]
        fn window_requested(
            self: Pin<&mut InstanceServer>,
            launch: bool,
            sidebar_visible: bool,
            backend: QString,
            model: QString,
            effort: QString,
            anonymous: i32,
            directory: QString,
        );
    }

    impl cxx_qt::Threading for InstanceServer {}
}

#[derive(Default)]
pub struct InstanceServerRust {
    socket_path: QString,
    listening: bool,
}

impl qobject::InstanceServer {
    fn set_socket_path(mut self: Pin<&mut Self>, path: QString) {
        if self.socket_path == path {
            return;
        }
        self.as_mut().rust_mut().socket_path = path.clone();
        self.as_mut().socket_path_changed();
        if self.listening || path.is_empty() {
            return;
        }
        let qt = self.qt_thread();
        let socket = std::path::PathBuf::from(path.to_string());
        let listened = clarp_core::instance::listen(&socket, move |arguments| {
            if qt.queue(move |server| server.request(arguments)).is_err() {
                eprintln!("InstanceServer: dropped a forwarded launch; the window is gone");
            }
        });
        match listened {
            Ok(()) => {
                self.as_mut().rust_mut().listening = true;
                self.as_mut().listening_changed();
            }
            // The launch still works, only later launches start their own process.
            Err(error) => eprintln!("InstanceServer: cannot listen on {}: {error}", socket.display()),
        }
    }

    fn request(self: Pin<&mut Self>, arguments: Vec<String>) {
        let options = match clarp_core::launch::parse(&arguments) {
            Ok(options) => options,
            Err(message) => {
                eprintln!("InstanceServer: ignored a forwarded launch: {message}");
                return;
            }
        };
        let setting = clarp_core::settings::Settings::user().boolean("launch/newAgentOnStartup", true);
        let launch = options.launch_on_startup(false, setting, false);
        let text = |value: &Option<String>| QString::from(value.as_deref().unwrap_or_default());
        self.window_requested(
            launch,
            options.no_new_agent,
            QString::from(&options.backend),
            text(&options.model),
            text(&options.effort),
            options.anonymous_mode(),
            text(&options.cwd),
        );
    }
}

impl Drop for InstanceServerRust {
    fn drop(&mut self) {
        // Later launches then start their own process instead of waiting on
        // a socket nobody answers.
        if self.listening
            && let Err(error) = std::fs::remove_file(self.socket_path.to_string())
        {
            eprintln!("InstanceServer: could not remove {}: {error}", self.socket_path);
        }
    }
}
