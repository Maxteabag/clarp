//! `clarp-host`: the Clarp Host.
//!
//! It reads its configuration, opens the fleet (which starts a thread for every agent and
//! rejoins the turns that are running), and serves the apps. Stopping it stops none of the
//! turns: they belong to their shims, and the next Host finds them.

use std::process::ExitCode;
use std::sync::{Arc, Mutex};

use clarp_host::config::{Config, USAGE};
use clarp_host::edge::{self, App};
use clarp_host::fleet::{self, Works};
use clarp_host::hub::Hub;
use clarp_host::ids;
use clarp_host::launcher::Launcher;
use clarp_host::owner::OwnerFile;
use clarp_host::pusher;
use clarp_host::relay::Relay;
use clarp_host::revisions::Revisions;
use clarp_host::telling::Telling;
use clarp_kernel::presence::Presence;
use clarp_ports::blobs::BlobEngine;
use clarp_ports::clock::{Clock, SystemClock};
use clarp_ports::kv::KeyValueEngine;
use clarp_ports::log::LogEngine;
use clarp_store_files::FileBlobs;
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};
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

/// The store of the files agents publish with this name in configuration.
fn blob_engine(name: &str) -> Option<Box<dyn BlobEngine>> {
    match name {
        "files" => Some(Box::new(FileBlobs::new())),
        "memory" => Some(Box::new(MemBlobs::new())),
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
    let blobs = blob_engine(&config.blobs)
        .ok_or_else(|| format!("there is no store of files named `{}`", config.blobs))?
        .open(&config.data_dir.join("media"))
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
    // The Host listens before its agents are opened: a turn that is rejoined, and every turn
    // after it, is told where the agent's own tools find the Host.
    let listener = tokio::net::TcpListener::bind(config.listen)
        .await
        .map_err(|error| format!("{}: {error}", config.listen))?;
    // The first line says where the Host listens: with port 0 only the Host knows.
    let address = listener.local_addr().map_err(|error| error.to_string())?;
    let revisions = Arc::new(Revisions::new());
    // What is to be pushed is taken from the first moment on; it goes out once the fleet,
    // which knows the phones, is there.
    let (pusher, to_push) = pusher::pusher();
    // And so is who has something to say: said once the one that speaks runs.
    let (speaks, to_speak) = clarp_host::voice::mouth();
    // What turns used is taken from the first moment on, and kept once the fleet is there.
    let (tally, used) = clarp_host::usage::tally();
    // Where this Host runs the owner's selectors of accounts itself: a pool for each tool
    // the owner has a selector for.
    let pools: Vec<Arc<clarp_host::pool::Service>> =
        if config.accounts == clarp_kernel::recovery::Accounts::Select {
            let owners = OwnerFile::new(config.speech_keys.clone()).read();
            let selectors = owners.as_ref().map(clarp_host::owner::selectors);
            let made = selectors.unwrap_or_default().into_iter();
            made.map(|(tool, command)| clarp_host::pool::Service::new(tool, command, clock.clone()))
                .collect()
        } else {
            Vec::new()
        };
    let works = Works {
        config: config.clone(),
        engine,
        codecs,
        launcher: Arc::new(launcher),
        clock: clock.clone(),
        hub: hub.clone(),
        revisions: revisions.clone(),
        tool_env: tool_env(&address.to_string(), &config.auth_token),
        pusher: pusher.clone(),
        blobs: blobs.clone(),
        speaks,
        // Who speaks for an agent is in the owner's file, where the keys of speech are.
        telling: Telling::new(OwnerFile::new(config.speech_keys.clone())),
        tally,
        pools: pools.clone(),
    };
    // The pictures this Host ships: for the list of agents, and for the face a push shows.
    let faces = Arc::new(clarp_host::faces::Faces::new(config.static_dir.clone()));
    let fleet = fleet::start(works, store)?;
    for pool in &pools {
        pool.attach(fleet.clone());
    }
    let limits: Arc<clarp_host::quota::Desk> = Arc::default();
    let counts = clarp_host::usage::Works {
        clock: clock.clone(),
        hub: hub.clone(),
        limits: limits.clone(),
        server_id: server_id.clone(),
    };
    tokio::spawn(clarp_host::usage::keep(fleet.clone(), used, counts));
    let desks = Arc::new(Mutex::new(Presence::new()));
    // The relay tells what paces the chip when it is asked too much, by an alert as well.
    let (limited, is_limited) = tokio::sync::mpsc::unbounded_channel();
    let relay = Some(config.push_relay.as_str())
        .filter(|relay| !relay.is_empty())
        .map(|relay| {
            Arc::new(Relay::new(
                relay,
                &config.push_credential,
                Some(limited.clone()),
            ))
        });
    to_push.run(pusher::Works {
        relay: relay.clone(),
        fleet: fleet.clone(),
        desks: desks.clone(),
        clock: clock.clone(),
        server_id: server_id.clone(),
        faces: faces.clone(),
        secret: config.auth_token.clone(),
    })?;
    let mouth = to_speak.run(clarp_host::voice::Works {
        config: config.clone(),
        fleet: fleet.clone(),
        hub: hub.clone(),
        clock: clock.clone(),
    });
    let app = App {
        config: config.clone(),
        hub,
        fleet,
        clock,
        mouth,
        faces,
        server_id,
        revisions,
        pusher,
        desks,
        blobs,
        activity: Arc::default(),
        calls: Arc::default(),
        chip: Arc::new(clarp_host::chip::Chip::new(relay, limited)),
        schedules: Arc::default(),
        heartbeat: Arc::default(),
        janitors: Arc::default(),
        router: Arc::default(),
        explain: Arc::default(),
        keeper: Arc::default(),
        audio: Arc::default(),
        limits,
        models: Arc::default(),
        door: Arc::default(),
        connector: Arc::new(clarp_host::connector::Connector::told(&config)),
        ears: {
            // The engines that listen. The Host's own loads its model from now on; answers
            // nobody will ask for again are forgotten on the side.
            let ears = Arc::new(clarp_host::heard::Ears::new(&config));
            let old = ears.clone();
            tokio::task::spawn_blocking(move || old.forget_old());
            ears
        },
    };
    // Where someone else puts another account in place when one is out, turns that wait for
    // an account are tried as soon as the tool's login has changed.
    if config.accounts == clarp_kernel::recovery::Accounts::Follow {
        let home = std::env::var_os("HOME").map_or_else(|| "/".into(), std::path::PathBuf::from);
        clarp_host::logins::follow(
            app.fleet.clone(),
            home,
            std::time::Duration::from_millis(config.logins_checked_every_ms),
        );
    }
    // The Host answers when it knows its agents: what they are doing, what runs for them. A
    // request that comes earlier waits at the door for that moment, it is not given half a view.
    let opened_by = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
    while tokio::time::Instant::now() < opened_by
        && app
            .fleet
            .directory()
            .members
            .iter()
            .any(|member| member.agent.view().broken.as_deref() == Some(clarp_host::agent::OPENING))
    {
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    // The janitors a Host has of its own are installed, each once, paused.
    if let Err(why) = clarp_host::janitors::install(&app).await {
        eprintln!("clarp-host: its own janitors were not installed: {}", why.0);
    }
    // A ring that rang when the Host stopped has its time again, or is missed now: the apps
    // and its agent are told without anybody having to ask first.
    clarp_host::calls::resume(&app);
    // And the chips of the phones are kept current from now on.
    clarp_host::chip::run(&app, is_limited);
    // And what is due of the schedules is sent.
    clarp_host::schedules::run(&app);
    // And an idle agent that asked for it is given a heartbeat when one is due.
    clarp_host::heartbeat::run(&app);
    // And a janitor's scheduled task is run when its schedule says.
    clarp_host::janitors::run(&app);
    // And what waits to be explained of the use of tools is asked of its janitor's model.
    clarp_host::explain::run(&app);
    // And the open goals are kept current beside their owners, where a keeper is on.
    clarp_host::keeper::run(&app);
    eprintln!("clarp-host listens on {address}");
    eprintln!(
        "clarp-host {} serves {} (engine {}, codec {}, blobs {}, launcher {})",
        clarp_host::VERSION,
        config.data_dir.display(),
        config.engine,
        config.codec,
        config.blobs,
        config.launcher
    );
    for pool in &pools {
        eprintln!(
            "clarp-host: the {} accounts: the owner's selector is run by this Host",
            pool.tool()
        );
    }
    // The edge is told who connected: the administrator's token opens the Host only from here.
    let router = edge::router(app.clone());
    // And where the Host is reached through a relay, what a phone asks there is asked of
    // the same routes, from now on.
    clarp_host::connector::run(&app, router.clone());
    let service = router.into_make_service_with_connect_info::<std::net::SocketAddr>();
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

/// What every turn is told of its Host: where it listens, and the token its agent's tools
/// present. Never the administrator's token. A skill's own script that speaks to its Host
/// by itself is told the same Host and the same token, under the names it reads: left to
/// itself it would find the Python Host of the machine, and ask there for an agent of this
/// one.
fn tool_env(address: &str, administrators: &str) -> Vec<(String, String)> {
    let mut env = vec![
        (clarp_host::TOOL_ADDR_ENV.to_string(), address.to_string()),
        (
            clarp_host::SCRIPT_URL_ENV.to_string(),
            format!("http://{address}"),
        ),
    ];
    let token = clarp_host::tool_token(administrators);
    if !token.is_empty() {
        env.push((clarp_host::TOOL_TOKEN_ENV.to_string(), token.clone()));
        env.push((clarp_host::SCRIPT_TOKEN_ENV.to_string(), token));
    }
    env
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
    if args.peek().map(String::as_str) == Some("network") {
        args.next();
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        return match clarp_host::admin::network(args, &token) {
            Ok(shown) => {
                println!("{shown}");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!(
                    "clarp-host network: {error}\n{}",
                    clarp_host::admin::NETWORK_USAGE
                );
                ExitCode::from(2)
            }
        };
    }
    if args.peek().map(String::as_str) == Some("peer") {
        args.next();
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        let peer_token = std::env::var("CLARP_PEER_TOKEN").unwrap_or_default();
        return match clarp_host::admin::peer(args, &token, &peer_token) {
            Ok(shown) => {
                println!("{shown}");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!(
                    "clarp-host peer: {error}\n{}",
                    clarp_host::admin::PEER_USAGE
                );
                ExitCode::from(2)
            }
        };
    }
    if args.peek().map(String::as_str) == Some("tool") {
        args.next();
        // What the turn was given for its agent's tools; the administrator's, when the
        // administrator calls a tool by hand.
        let token = std::env::var(clarp_host::TOOL_TOKEN_ENV)
            .ok()
            .filter(|token| !token.is_empty())
            .or_else(|| std::env::var("CLARP_HOST_TOKEN").ok())
            .unwrap_or_default();
        let outcome = clarp_host::admin::tool(args, &token);
        print!("{}", outcome.stdout);
        eprint!("{}", outcome.stderr);
        return ExitCode::from(outcome.exit);
    }
    if args.peek().map(String::as_str) == Some("import") {
        args.next();
        let source = std::env::var("CLARP_SOURCE_TOKEN").unwrap_or_default();
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        let files = std::env::var("CLARP_FILES_TOKEN").unwrap_or_default();
        return match clarp_host::admin::import(args, &source, &token, &files) {
            Ok(shown) => {
                println!("{shown}");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!(
                    "clarp-host import: {error}\n{}",
                    clarp_host::admin::IMPORT_USAGE
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
