//! How the window was asked to open (`clarp_core::launch` options), and one
//! process per desktop (`clarp_core::instance`): a second launch of this
//! build, Host and display hands its arguments to the running window and
//! exits; the window shows itself and starts the agent asked for.

use clarp_core::launch::LaunchOptions;

/// A new agent the window should start once it can (the launch arguments,
/// or the "start a new agent when opening Clarp" setting).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Request {
    pub backend: String,
    pub model: String,
    pub effort: String,
    /// 1 anonymous, 0 contact, -1 as Settings say.
    pub anonymous: i32,
    pub directory: String,
}

impl Request {
    pub fn from_options(options: &LaunchOptions) -> Self {
        Self {
            backend: options.backend.clone(),
            model: options.model.clone().unwrap_or_default(),
            effort: options.effort.clone().unwrap_or_default(),
            anonymous: options.anonymous_mode(),
            directory: options.cwd.clone().unwrap_or_default(),
        }
    }
}

/// Parses the command line; prints help or the version and exits for those.
pub fn options(arguments: &[String]) -> LaunchOptions {
    let options = match clarp_core::launch::parse(arguments) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(1);
        }
    };
    if options.help {
        print!("{}", clarp_core::launch::HELP);
        std::process::exit(0);
    }
    if options.version {
        println!("clarp-slint {}", env!("CARGO_PKG_VERSION"));
        std::process::exit(0);
    }
    options
}

/// Hands this launch to a running window and exits when one takes it;
/// otherwise returns the socket this process should listen on.
pub fn forward_or_socket(arguments: &[String]) -> Option<std::path::PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let socket = clarp_core::instance::socket_path(arguments, &executable, |name| std::env::var(name).ok())?;
    if clarp_core::instance::forward(&socket, arguments) {
        std::process::exit(0);
    }
    Some(socket)
}

/// Listens for later launches; each shows the window and starts what it asks.
pub fn listen(socket: &std::path::Path) {
    let listened = clarp_core::instance::listen(socket, |arguments| {
        if let Err(error) = slint::invoke_from_event_loop(move || forwarded(arguments)) {
            eprintln!("clarp-slint: dropped a forwarded launch: {error}");
        }
    });
    if let Err(error) = listened {
        // This launch still works; only later launches start their own process.
        eprintln!("clarp-slint: cannot listen on {}: {error}", socket.display());
    }
}

fn forwarded(arguments: Vec<String>) {
    use slint::ComponentHandle;
    let options = match clarp_core::launch::parse(&arguments) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("clarp-slint: ignored a forwarded launch: {message}");
            return;
        }
    };
    let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
    if let Err(error) = window.show() {
        eprintln!("clarp-slint: cannot show the window: {error}");
    }
    let setting = app.engine.borrow().settings().boolean("launch/newAgentOnStartup", false);
    if options.launch_on_startup(false, setting, false) {
        start(&app, &window, Request::from_options(&options));
    }
}

/// Starts `request` in the window: the New Session hub, or at once when it
/// names a backend.
pub fn start(app: &std::rc::Rc<crate::App>, window: &crate::AppWindow, request: Request) {
    *app.launch.borrow_mut() = Some(request.clone());
    // Anonymous or contact as asked, else as Settings say.
    let anonymous = match request.anonymous {
        1 => true,
        0 => false,
        _ => app.engine.borrow().settings().boolean("launch/anonymousAgents", true),
    };
    crate::launch_view::open_launch(app, window, &request.backend, &request.model, &request.effort, anonymous, &request.directory);
}
