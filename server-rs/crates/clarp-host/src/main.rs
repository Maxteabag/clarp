//! `clarp-host`: the Clarp Host.
//!
//! It reads its configuration, opens the fleet (which starts a thread for every agent and
//! rejoins the turns that are running), and serves the apps. Stopping it stops none of the
//! turns: they belong to their shims, and the next Host finds them.

use std::process::ExitCode;
use std::sync::Arc;

use clarp_host::config::{Config, USAGE};
use clarp_host::edge::{self, App};
use clarp_host::fleet::{self, Works};
use clarp_host::hub::Hub;
use clarp_host::ids;
use clarp_host::launcher::Launcher;
use clarp_ports::clock::{Clock, SystemClock};
use clarp_ports::kv::KeyValueEngine;
use clarp_ports::log::LogEngine;
use clarp_store_mem::{MemEngine, MemKeyValue};
use clarp_store_sqlite::{SqliteEngine, SqliteKeyValue};

const SERVER_ID: &str = "server_id";

/// The engines with this name in configuration: one for the agents' logs, one for the fleet.
fn engines(name: &str) -> Option<(Arc<dyn LogEngine>, Box<dyn KeyValueEngine>)> {
    match name {
        "sqlite" => Some((
            Arc::new(SqliteEngine::new()),
            Box::new(SqliteKeyValue::new()),
        )),
        "memory" => Some((Arc::new(MemEngine::new()), Box::new(MemKeyValue::new()))),
        _ => None,
    }
}

async fn serve(config: Config) -> Result<(), String> {
    let config = Arc::new(config);
    let clock: Arc<dyn Clock + Send + Sync> = Arc::new(SystemClock);
    let (engine, tables) = engines(&config.engine)
        .ok_or_else(|| format!("there is no engine named `{}`", config.engine))?;
    let codecs = clarp_codec::standard_set(&config.codec)
        .ok_or_else(|| format!("there is no codec named `{}`", config.codec))?;
    let launcher = Launcher::by_name(&config.launcher, config.shim.clone())
        .ok_or_else(|| format!("there is no launcher named `{}`", config.launcher))?;
    std::fs::create_dir_all(&config.data_dir)
        .and_then(|()| std::fs::create_dir_all(&config.run_dir))
        .map_err(|error| format!("the Host's directories cannot be made: {error}"))?;

    let mut store = tables
        .open(&config.data_dir.join("fleet.db"))
        .map_err(|error| error.to_string())?;
    // A Host is the same Host for as long as it has its data.
    let server_id = match store.get("setting", SERVER_ID).map_err(|e| e.to_string())? {
        Some(known) => String::from_utf8_lossy(&known).into_owned(),
        None => {
            let minted = ids::uuid();
            store
                .put("setting", SERVER_ID, minted.as_bytes())
                .map_err(|error| error.to_string())?;
            minted
        }
    };

    let hub = Arc::new(Hub::new(clock.now_ms()));
    // The first event of a Host's life says which version it is. A client that connects while
    // the hub still keeps it learns that the Host was started again, and with what.
    hub.publish(
        clarp_host::wire::server_version(clarp_host::VERSION, clock.now_ms()),
        clock.now_ms(),
    );
    let works = Works {
        config: config.clone(),
        engine,
        codecs,
        launcher: Arc::new(launcher),
        clock: clock.clone(),
        hub: hub.clone(),
    };
    let fleet = fleet::start(works, store)?;
    let app = App {
        config: config.clone(),
        hub,
        fleet,
        clock,
        server_id,
    };

    let listener = tokio::net::TcpListener::bind(config.listen)
        .await
        .map_err(|error| format!("{}: {error}", config.listen))?;
    // The first line says where the Host listens: with port 0 only the Host knows.
    let address = listener.local_addr().map_err(|error| error.to_string())?;
    eprintln!("clarp-host listens on {address}");
    eprintln!(
        "clarp-host {} serves {} (engine {}, codec {}, launcher {})",
        clarp_host::VERSION,
        config.data_dir.display(),
        config.engine,
        config.codec,
        config.launcher
    );
    // The edge is told who connected: the administrator's token opens the Host only from here.
    let service = edge::router(app).into_make_service_with_connect_info::<std::net::SocketAddr>();
    axum::serve(listener, service)
        .with_graceful_shutdown(told_to_stop())
        .await
        .map_err(|error| error.to_string())
}

/// Resolves when the Host is asked to stop. The turns that run are not asked: they go on.
async fn told_to_stop() {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate()).ok();
    let terminated = async {
        match terminate.as_mut() {
            Some(terminate) => {
                terminate.recv().await;
            }
            None => std::future::pending::<()>().await,
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        () = terminated => {}
    }
}

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1).peekable();
    if args.peek().map(String::as_str) == Some("pair") {
        args.next();
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        return match clarp_host::admin::pair(args, &token) {
            Ok(shown) => {
                println!("{shown}");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!(
                    "clarp-host pair: {error}\n{}",
                    clarp_host::admin::PAIR_USAGE
                );
                ExitCode::from(2)
            }
        };
    }
    let config = match Config::from_args(args) {
        Ok(config) => config,
        Err(error) => {
            eprintln!("clarp-host: {error}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("clarp-host: the runtime could not be started: {error}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(serve(config)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("clarp-host: {error}");
            ExitCode::FAILURE
        }
    }
}
