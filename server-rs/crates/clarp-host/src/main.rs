//! `clarp-host`: the Clarp Host.
//!
//! It reads its configuration, opens the fleet (which starts a thread for every agent and
//! rejoins the turns that are running), and serves the apps. Stopping it stops none of the
//! turns: they belong to their shims, and the next Host finds them.

mod ingress;
mod shutdown;

use std::process::ExitCode;
use std::sync::{Arc, Mutex};

use clarp_host::config::{Config, USAGE};
use clarp_host::edge::{self, App};
use clarp_host::fleet::{self, Works};
use clarp_host::hub::Hub;
use clarp_host::ids;
use clarp_host::launcher::Launcher;
use clarp_host::owner::OwnerFile;
use clarp_host::pairing::{self, SERVER_ID};
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

fn host_clock() -> Arc<dyn Clock + Send + Sync> {
    #[cfg(debug_assertions)]
    if let Some(file) = std::env::var_os("CLARP_TEST_CLOCK_OFFSET_FILE") {
        return Arc::new(FixtureClock(file.into()));
    }
    Arc::new(SystemClock)
}

/// The agents' logs as the Host keeps them. The integration tests can make each commit to them
/// take a while, as a sync does on a disk under pressure (issue 831).
fn slow_disk(engine: Arc<dyn LogEngine>) -> Arc<dyn LogEngine> {
    #[cfg(debug_assertions)]
    if let Some(ms) = std::env::var("CLARP_TEST_APPEND_DELAY_MS")
        .ok()
        .and_then(|ms| ms.parse().ok())
    {
        return Arc::new(SlowDisk {
            inner: engine,
            delay: std::time::Duration::from_millis(ms),
        });
    }
    engine
}

/// Logs each of whose commits waits a while before it is made.
#[cfg(debug_assertions)]
struct SlowDisk {
    inner: Arc<dyn LogEngine>,
    delay: std::time::Duration,
}

#[cfg(debug_assertions)]
impl LogEngine for SlowDisk {
    fn name(&self) -> &'static str {
        self.inner.name()
    }

    fn open(
        &self,
        agent_dir: &std::path::Path,
        codecs: clarp_ports::codec::CodecSet,
    ) -> Result<Box<dyn clarp_ports::log::AgentLog>, clarp_ports::log::LogError> {
        Ok(Box::new(SlowLog {
            inner: self.inner.open(agent_dir, codecs)?,
            delay: self.delay,
        }))
    }
}

#[cfg(debug_assertions)]
struct SlowLog {
    inner: Box<dyn clarp_ports::log::AgentLog>,
    delay: std::time::Duration,
}

#[cfg(debug_assertions)]
impl clarp_ports::log::AgentLog for SlowLog {
    fn append(
        &mut self,
        items: &[clarp_ports::log::NewItem],
    ) -> Result<clarp_ports::log::Seq, clarp_ports::log::LogError> {
        std::thread::sleep(self.delay);
        self.inner.append(items)
    }
    fn last_seq(&self) -> Result<clarp_ports::log::Seq, clarp_ports::log::LogError> {
        self.inner.last_seq()
    }
    fn read_after(
        &self,
        after: clarp_ports::log::Seq,
        kinds: clarp_ports::log::Kinds,
        limit: usize,
    ) -> Result<Vec<clarp_ports::log::Item>, clarp_ports::log::LogError> {
        self.inner.read_after(after, kinds, limit)
    }
    fn read_before(
        &self,
        before: clarp_ports::log::Seq,
        kinds: clarp_ports::log::Kinds,
        limit: usize,
    ) -> Result<Vec<clarp_ports::log::Item>, clarp_ports::log::LogError> {
        self.inner.read_before(before, kinds, limit)
    }
    fn find(
        &self,
        key: &str,
    ) -> Result<Option<clarp_ports::log::Item>, clarp_ports::log::LogError> {
        self.inner.find(key)
    }
    fn with_key_prefix(
        &self,
        prefix: &str,
        kinds: clarp_ports::log::Kinds,
    ) -> Result<Vec<clarp_ports::log::Item>, clarp_ports::log::LogError> {
        self.inner.with_key_prefix(prefix, kinds)
    }
    fn read_tail(
        &self,
        kinds: clarp_ports::log::Kinds,
        limit: usize,
    ) -> Result<Vec<clarp_ports::log::Item>, clarp_ports::log::LogError> {
        self.inner.read_tail(kinds, limit)
    }
}

/// The integration tests start the real Host in a child process and advance its clock
/// through a fixture file, so a minute of cache expiry takes no wall time.
#[cfg(debug_assertions)]
struct FixtureClock(std::path::PathBuf);

#[cfg(debug_assertions)]
impl Clock for FixtureClock {
    fn now_ms(&self) -> i64 {
        let offset = std::fs::read_to_string(&self.0)
            .ok()
            .and_then(|text| text.trim().parse().ok())
            .unwrap_or_default();
        SystemClock.now_ms().saturating_add(offset)
    }
}

/// How long each step of a start took, from the moment `serve` began, said in one line of the
/// journal once the Host answers (issue 935): a start that keeps the apps waiting is told by its
/// steps, not guessed from the lines around it.
struct Steps {
    began: std::time::Instant,
    marks: Vec<(&'static str, u128)>,
}

impl Steps {
    fn new() -> Self {
        Self {
            began: std::time::Instant::now(),
            marks: Vec::new(),
        }
    }

    fn mark(&mut self, step: &'static str) {
        self.marks.push((step, self.began.elapsed().as_millis()));
    }

    fn line(&self) -> String {
        let steps: Vec<String> = self
            .marks
            .iter()
            .map(|(step, ms)| format!("{step}={ms}"))
            .collect();
        format!(
            "clarp-host: its start, ms since it began: {}",
            steps.join(" ")
        )
    }
}

/// How many agents, janitors among them, are still being opened.
fn still_opening(app: &App) -> usize {
    let directory = app.fleet.directory();
    directory
        .members
        .iter()
        .chain(directory.janitors.iter())
        .filter(|member| member.agent.view().broken.as_deref() == Some(clarp_host::agent::OPENING))
        .count()
}

async fn serve(config: Config) -> Result<(), String> {
    let mut steps = Steps::new();
    // The socket its unit holds across a restart, taken before the Host starts any process, so
    // that none inherits it (issue 935).
    let inherited = ingress::inherited(config.listen)?;
    let held_by_unit = inherited.is_some();
    // Capture the configured store paths before any child can be launched.
    clarp_host::child_environment();
    let config = Arc::new(config);
    let clock = host_clock();
    // The turns that start their tool anew, counted for the apps; the agents the fleet opens
    // count in it too.
    let cold_starts = clarp_host::cold_starts::ColdStarts::new(clock.clone());
    clarp_host::cold_starts::install(cold_starts.clone());
    let (engine, tables) = engines(&config.engine)
        .ok_or_else(|| format!("there is no engine named `{}`", config.engine))?;
    let engine = slow_disk(engine);
    let codecs = clarp_codec::standard_set(&config.codec)
        .ok_or_else(|| format!("there is no codec named `{}`", config.codec))?;
    let launcher = Launcher::by_name(&config.launcher, config.shim.clone())
        .ok_or_else(|| format!("there is no launcher named `{}`", config.launcher))?;
    std::fs::create_dir_all(&config.data_dir)
        .and_then(|()| std::fs::create_dir_all(&config.run_dir))
        .map_err(|error| format!("the Host's directories cannot be made: {error}"))?;
    // Before anything in them is opened: what SQLite writes there from now on is written in
    // place, without compression (issue 848).
    clarp_host::data_flags::tell(&clarp_host::data_flags::at_start(&config.data_dir));
    steps.mark("directories");

    if let Err(reason) = clarp_host::calls::generation::bootstrap(&config.data_dir) {
        eprintln!(
            "clarp-host: listen-first generation unavailable ({})",
            reason.kind()
        );
    }

    steps.mark("generation");
    clarp_host::once::sweep_routing(&config.run_dir, std::time::SystemTime::now());
    steps.mark("routing");
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
    let previous_server_ids =
        pairing::previous_server_ids(store.as_ref()).map_err(|error| error.to_string())?;
    // What signs the token every turn is given: the Host's own key, kept before any turn starts.
    let turns = Arc::new(clarp_host::turn_identity::Signer::open(store.as_mut()));

    use clarp_kernel::diagnostics::{KEY, Settings};
    use clarp_ports::diagnostics::DiagnosticStore;
    let setting = store
        .get("setting", KEY)
        .ok()
        .flatten()
        .and_then(|v| String::from_utf8(v).ok())
        .map_or_else(Settings::default, |s| Settings::decode(&s));
    let mut files = clarp_store_files::diagnostics::HourFiles::new(
        config.data_dir.join("diagnostics"),
        clarp_host::diagnostics::CAP,
    );
    files
        .append(clock.now_ms(), &[])
        .map_err(|e| e.to_string())?;
    let capture = clarp_host::diagnostics::Capture::start(
        Box::new(files),
        clock.clone(),
        setting,
        config.data_dir.clone(),
    );
    steps.mark("store");
    // Its events' numbers are reserved on disk before any is given out, above every number an
    // earlier run may have given out however it ended (issue 935): a client resuming from an
    // earlier run's number is told it missed something.
    let mut hub =
        Hub::new(clock.now_ms()).preparing_reservation(config.data_dir.join("event-numbers"));
    // Only read the previous floor here. The new block is written beside the HTTP server.
    steps.mark("events");
    hub.capture = Some(capture.clone());
    let hub = Arc::new(hub);
    // The first event of a Host's life says which version it is. A client that connects while
    // the hub still keeps it learns that the Host was started again, and with what.
    hub.publish(
        clarp_host::wire::server_version(clarp_host::VERSION, clock.now_ms()),
        clock.now_ms(),
    );
    // The Host listens before its agents are opened: a turn that is rejoined, and every turn
    // after it, is told where the agent's own tools find the Host.
    let listener = ingress::listen(config.listen, inherited).await?;
    // The first line says where the Host listens: with port 0 only the Host knows.
    let address = listener.local_addr().map_err(|error| error.to_string())?;
    steps.mark("bound");
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
            let mut made = selectors.unwrap_or_default();
            if !config.seats_tool.is_empty() {
                for tool in ["claude", "codex"] {
                    if !made.iter().any(|(t, _)| *t == tool) {
                        made.push((tool, Vec::new()));
                    }
                }
            }
            made.into_iter()
                .map(|(tool, command)| {
                    let lock = (!config.seats_tool.is_empty())
                        .then(|| config.data_dir.join(format!("account-{tool}.lock")));
                    clarp_host::pool::Service::with_lock(tool, command, clock.clone(), lock)
                })
                .collect()
        } else {
            Vec::new()
        };
    let (opening_gate, opening_requests) = clarp_host::calls::opening_launch::channel();
    let gallery_db = tables
        .open(&config.data_dir.join("gallery.db"))
        .map_err(|error| error.to_string())?;
    let gallery = clarp_host::gallery::Store::open(gallery_db, blobs.clone())
        .map_err(|error| error.words().to_owned())?;
    let gallery = clarp_host::gallery::Writer::start(gallery).map_err(|error| error.to_string())?;
    let team_disk = tables
        .open(&config.data_dir.join("teams.db"))
        .map_err(|error| error.to_string())?;
    let teams =
        Arc::new(clarp_host::teams::Desk::open(team_disk).map_err(|error| error.to_string())?);
    // Where a plain send of the owner's is kept while its agent is opened (issue 935): made
    // before any agent opens, which takes what was kept for it. Its store is opened by its own
    // thread, beside the start: a disk that is slow to make it holds no answer of the Host's.
    {
        let (engine, path) = (config.engine.clone(), config.data_dir.join("accepted.db"));
        let at = path.clone();
        clarp_host::accepted::install(
            &path,
            Box::new(move || {
                let (_, tables) = engines(&engine)
                    .ok_or_else(|| format!("there is no engine named `{engine}`"))?;
                tables.open(&at).map_err(|error| error.to_string())
            }),
        )?;
    }
    let works = Works {
        teams: Some(teams.clone()),
        runtime: tokio::runtime::Handle::current(),
        opening_gate,
        config: config.clone(),
        engine,
        codecs,
        launcher: Arc::new(launcher),
        clock: clock.clone(),
        hub: hub.clone(),
        revisions: revisions.clone(),
        tool_env: clarp_host::tool_env(&address.to_string(), &config.auth_token),
        pusher: pusher.clone(),
        blobs: blobs.clone(),
        portrait_hints: Some(clarp_host::media_created::Announcer {
            writer: gallery.clone(),
            hub: hub.clone(),
            clock: clock.clone(),
            log: clarp_host::unkept::Log::stderr(clock.clone()),
        }),
        speaks,
        // Who speaks for an agent is in the owner's file, where the keys of speech are.
        telling: Telling::new(OwnerFile::new(config.speech_keys.clone())),
        tally,
        turns: turns.clone(),
        pools: pools.clone(),
        log: clarp_host::unkept::Log::stderr(clock.clone()),
        left_out: Arc::default(),
    };
    // The pictures this Host ships: for the list of agents, and for the face a push shows.
    let faces = Arc::new(clarp_host::faces::Faces::new(config.static_dir.clone()));
    steps.mark("stores");
    // Every agent's handle is there; its thread begins once the Host answers (issue 935): what
    // the start reads of its stores before it answers is read before the agents' logs keep the
    // disk busy.
    let (fleet, unopened) = fleet::read(works, store)?;
    steps.mark("fleet");
    for pool in &pools {
        pool.attach(fleet.clone());
    }
    let limits: Arc<clarp_host::quota::Desk> = Arc::default();
    for pool in &pools {
        pool.watch_limits(limits.clone());
    }
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
    let direct = Arc::new(clarp_host::direct::Direct::new(
        OwnerFile::new(config.push_keys.clone()),
        &config.apns_endpoint,
        clock.clone(),
    ));
    let android = Arc::new(clarp_host::fcm::Sender::new(
        OwnerFile::new(config.push_keys.clone()),
        Arc::new(clarp_host::fcm::Http::new(config.fcm_endpoint.clone())),
        clock.clone(),
    ));
    to_push.run(pusher::Works {
        android: android.clone(),
        relay: relay.clone(),
        fleet: fleet.clone(),
        desks: desks.clone(),
        clock: clock.clone(),
        server_id: server_id.clone(),
        faces: faces.clone(),
        secret: config.auth_token.clone(),
        // The key the owner gave for the push service is named in the file he names for
        // it; without that word nothing is handed to the service.
        direct: direct.clone(),
        runtime: tokio::runtime::Handle::current(),
    })?;
    let mouth = to_speak.run(clarp_host::voice::Works {
        config: config.clone(),
        fleet: fleet.clone(),
        hub: hub.clone(),
        clock: clock.clone(),
    });
    let ears = Arc::new(clarp_host::heard::Ears::new(&config, &fleet).await?);
    steps.mark("ears");
    let client_events =
        clarp_host::client_events::Desk::new(config.data_dir.join("client-events.sqlite"));
    client_events.age_out(clock.clone());
    let app = App {
        client_events,
        teams,
        capture,
        turns,
        form_event_writer: Arc::default(),
        skill_links: Arc::default(),
        controller_actions: Arc::default(),
        calendar_receipts: Arc::default(),
        android_chip: Arc::default(),
        android,
        config: config.clone(),
        hub,
        fleet,
        clock,
        mouth,
        faces,
        gallery,
        server_id,
        previous_server_ids,
        revisions,
        pusher,
        desks,
        blobs,
        activity: Arc::default(),
        calls: Arc::default(),
        chip: Arc::new(clarp_host::chip::Chip::new(relay, direct, limited)),
        schedules: Arc::default(),
        heartbeat: Arc::default(),
        janitors: Arc::default(),
        fallbacks: Arc::default(),
        router: Arc::default(),
        explain: Arc::default(),
        experience: Arc::default(),
        oracle: Arc::default(),
        keeper: Arc::default(),
        quota_keeper: Arc::default(),
        cold_starts,
        start: Arc::new(clarp_host::start::Readiness::starting()),
        label_checker: Arc::default(),
        audio: Arc::default(),
        limits,
        // A Host that keeps the logins itself lets the owner's tool renew one as it reads.
        pools: pools.clone(),
        seats: Arc::new(clarp_host::seats::Desk::new(
            &config.seats_tool,
            config.accounts == clarp_kernel::recovery::Accounts::Select,
        )),
        models: Arc::default(),
        sign_ins: Arc::default(),
        door: Arc::default(),
        connector: Arc::new(clarp_host::connector::Connector::told(&config)),
        // The index in its own folder (issue 848), made anew there once: the one beside the
        // fleet's file before is removed once the new one has opened.
        search: Arc::new(clarp_host::search::Desk::replacing(
            config
                .data_dir
                .join(clarp_host::data_flags::SEARCH_FOLDER)
                .join(clarp_host::search::SEARCH_DB),
            config.data_dir.join(clarp_host::search::SEARCH_DB),
        )),
        ears: {
            // The engines that listen. The Host's own loads its model from now on; answers
            // nobody will ask for again are forgotten on the side.
            let old = ears.clone();
            tokio::task::spawn_blocking(move || old.forget_old());
            ears
        },
    };
    // Every event of the roster about one agent carries how it sounds and is heard.
    let profiles = clarp_host::profile::Profiles::new(&app.mouth, &app.ears);
    app.hub
        .profiles
        .set(profiles)
        .map_err(|_| "voice profiles were already connected".to_string())?;
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
    steps.mark("app");
    // The Host answers from here on (issue 935). A release stops one Host and starts the next,
    // and the apps waited the whole start of the next one, 45 seconds on the owner's machine,
    // for anything at all. Everything the start does after this goes on beside the answers, in
    // the order it always had. What a request needs of the start, it waits for at the door
    // (`clarp_host::start`): liveness is answered at once, the agents' rows once every agent is
    // open, everything else once the start is done; a request is held there, never refused.
    // The edge is told who connected: the administrator's token opens the Host only from here.
    let router = edge::router(app.clone());
    // And where the Host is reached through a relay, what a phone asks there is asked of
    // the same routes, from now on.
    clarp_host::connector::run(&app, router.clone());
    // Whether each agent can answer now is told as it changes; and how long the Host took to
    // come back since it was last told to stop is kept, for what it says when it is told again.
    clarp_host::availability::run(&app);
    let restart_ms = Arc::new(std::sync::atomic::AtomicI64::new(
        clarp_host::availability::RESTART_DEFAULT_MS,
    ));
    let mut server = tokio::spawn(shutdown::serve(
        listener,
        router,
        app.clone(),
        restart_ms.clone(),
    ));
    steps.mark("answering");
    app.hub.reserve_at_start();
    // How long this Host was away is kept beside the answers: a write that waits for the disk
    // holds none of them (issue 935).
    {
        let (data, now) = (app.config.data_dir.clone(), app.clock.now_ms());
        tokio::task::spawn_blocking(move || {
            clarp_host::availability::host_started(&data, now);
            let estimate = clarp_host::availability::restart_estimate(&data);
            restart_ms.store(estimate, std::sync::atomic::Ordering::Relaxed);
        });
    }
    let finishing = async {
        // Identity can answer while the reservation is written. Keep the agents'
        // opening reads and writes behind the first reservation attempt.
        app.hub.event_numbers_ready().await;
        unopened.begin();
        // The tests make a start fail after the Host has begun to answer (issue 935): once the
        // file named exists.
        #[cfg(debug_assertions)]
        if let Some(when) = std::env::var_os("CLARP_TEST_START_FAILS_WHEN") {
            let when = std::path::PathBuf::from(when);
            while !when.exists() {
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
            return Err("a step of the start failed (CLARP_TEST_START_FAILS_WHEN)".to_string());
        }
        // The janitors a Host has of its own are installed, each once, paused. Before anything
        // waits for the agents' rows: with these, every agent the start admits is in the
        // roster, and "every agent is open" means all of them (issue 935).
        if let Err(why) = clarp_host::janitors::install(&app).await {
            eprintln!("clarp-host: its own janitors were not installed: {}", why.0);
        }
        steps.mark("janitors");
        // The roster answers once every agent's row is its own: read from what its log
        // committed, or open (issue 935). Said once, with when, from beside the start.
        {
            let app = app.clone();
            let began = steps.began;
            tokio::spawn(async move {
                let directory = app.fleet.directory();
                for member in directory.members.iter().chain(directory.janitors.iter()) {
                    member.agent.row_is_its_own().await;
                }
                app.start.reach(clarp_host::start::Stage::Rows);
                eprintln!(
                    "clarp-host: every agent's row was its own {} ms after the start began",
                    began.elapsed().as_millis()
                );
            });
        }
        // Where this Host keeps the machine's tools, the chosen skills are linked to its release;
        // anywhere else this writes nothing. A turn that starts before they are finds the release
        // before's skills, which install-host keeps until the links have moved.
        clarp_host::managed_skills::at_start(&app).await;
        steps.mark("skills");
        // What follows needs the agents opened: it waits for them, twenty seconds at most.
        let opened_by = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        while tokio::time::Instant::now() < opened_by
            && app.fleet.directory().members.iter().any(|member| {
                member.agent.view().broken.as_deref() == Some(clarp_host::agent::OPENING)
            })
        {
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        }
        steps.mark("door");
        // How many agents were still being opened when the start's wait for them ended, and when
        // the last of them was (issue 935): one line, from beside the start, which goes on.
        {
            let app = app.clone();
            let began = steps.began;
            tokio::spawn(async move {
                let at_door = still_opening(&app);
                while still_opening(&app) > 0 {
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                }
                eprintln!(
                    "clarp-host: {at_door} agents were still being opened after the start's wait; the last was \
                     opened {} ms after the start began",
                    began.elapsed().as_millis()
                );
                // Every agent the start admitted, its own janitors included, is open: every row
                // is its own from now on, and what asks about them is answered.
                app.start.reach(clarp_host::start::Stage::Agents);
            });
        }
        app.mouth.start_voice_names().await;
        steps.mark("voice_names");
        clarp_host::profile::migrate_at_start(&app).await;
        steps.mark("voice_shapes");
        clarp_host::media_manage::import_history(&app)
            .await
            .map_err(|e| e.words().to_owned())?;
        steps.mark("gallery");
        clarp_host::portrait_generation::recover(&app)
            .await
            .map_err(|e| e.0)?;
        clarp_host::media_manage::recover(&app).await;
        steps.mark("media");
        // A ring that rang when the Host stopped has its time again, or is missed now: the apps
        // and its agent are told without anybody having to ask first.
        // Repair admissions before speech queued by surviving shims can be voiced.
        // What recovers, later, the call of an agent that does not answer now.
        let recovering = clarp_host::calls::opening::Recovering(app.clone());
        app.mouth.held.recovered_by(Arc::new(recovering));
        steps.mark("rings");
        app.calls.recover_bindings(&app).await.map_err(|e| e.0)?;
        steps.mark("calls");
        // A ring on an event stream is judged by these from now on (issue 935).
        app.start.recovered_calls();
        app.mouth.bindings_ready.notify_one();
        clarp_host::calls::resume(&app);
        let staging = app.clone();
        tokio::spawn(async move {
            let hour = std::time::Duration::from_secs(3600);
            let mut tick = tokio::time::interval_at(tokio::time::Instant::now() + hour, hour);
            loop {
                tick.tick().await;
                if staging
                    .ears
                    .sweep_model_staging(&staging.fleet)
                    .await
                    .is_err()
                {
                    eprintln!("clarp-host: could not sweep orphaned transcription staging");
                }
            }
        });
        let pruning = app.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(std::time::Duration::from_secs(60));
            loop {
                tick.tick().await;
                if let Err(why) = pruning.calls.prune_openings(&pruning).await {
                    eprintln!("clarp-host: opening retention: {}", why.0);
                }
            }
        });
        // And the chips of the phones are kept current from now on.
        tokio::spawn(clarp_host::calls::opening_launch::run(
            app.clone(),
            opening_requests,
        ));
        clarp_host::chip::run(&app, is_limited);
        tokio::spawn(clarp_host::android_chip::run(app.clone()));
        // A contact's picture that was being made when the Host stopped ends failed, as a try:
        // nothing is asked of a provider at a start.
        {
            let app = app.clone();
            tokio::spawn(async move { clarp_host::pictures::interrupted(&app).await });
        }
        // Idle lanes leave while memory is short.
        clarp_host::watch_lane_pressure(&app);
        // And what is due of the schedules is sent.
        clarp_host::schedules::run(&app);
        clarp_host::experience_watch::run(&app);
        // And the conversations are kept searchable, their history read in the background.
        clarp_host::search::run(&app);
        // And a handoff of the Oracle's that a phone does not get on with is not waited for.
        clarp_host::oracle::run(&app);
        // And an idle agent that asked for it is given a heartbeat when one is due.
        clarp_host::heartbeat::run(&app);
        clarp_host::teams::runtime::run(&app);
        clarp_host::goal_listener::run(&app);
        // And a janitor's scheduled task is run when its schedule says.
        clarp_host::janitors::run(&app);
        clarp_host::seats::start_switcher(&app);
        // And whether an account is used up or near a limit is looked at again as it may change.
        clarp_host::quota::watch(&app);
        // And what waits to be explained of the use of tools is asked of its janitor's model.
        clarp_host::explain::run(&app);
        // And the open goals are kept current beside their owners, where a keeper is on.
        clarp_host::keeper::run(&app);
        // And an idle agent's own label that outlived its time is taken back.
        clarp_host::labels::run(&app);
        steps.mark("started");
        // Said once the start has finished. The Host has answered since `answering`; whoever
        // waits for this line (the tests, an installer) finds the whole start done.
        eprintln!("clarp-host listens on {address}");
        // What could not be read of what it keeps, in one line: the door shows the same counts.
        // Said after where it listens, which is the Host's first line.
        eprintln!("{}", app.fleet.left_out().line());
        if held_by_unit {
            eprintln!(
                "clarp-host: its port is held by its socket unit: what comes in while it restarts waits for it"
            );
        }
        if app.config.peers.is_none() {
            eprintln!("clarp-host: started without --peers: this Host has no peers");
        }
        if app.config.config_dir.is_none() {
            eprintln!(
                "clarp-host: started without --config: every owner's setting is its default, and \
                 no account reset command runs"
            );
        }
        if let Some(why) = app.turns.unkept() {
            app.fleet.unkept(clarp_host::turn_identity::KIND, why);
        } else if !app.turns.has_key() {
            eprintln!(
                "clarp-host: the key of the turns' tokens cannot be read: no turn is given one, and a \
                 token presented is answered that it cannot be said whose it is"
            );
        }
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
        // The start is done once every agent is open too: what waits for that is answered now.
        app.start.reached(clarp_host::start::Stage::Agents).await;
        app.start.reach(clarp_host::start::Stage::Started);
        steps.mark("ready");
        // Said last of its start, after every line the start has always said.
        eprintln!("{}", steps.line());
        Ok::<(), String>(())
    };
    // A start that fails ends the Host, as before; a Host told to stop while it starts stops.
    tokio::select! {
        finished = finishing => finished?,
        served = &mut server => {
            return served
                .map_err(|error| error.to_string())?
                .map_err(|error| error.to_string());
        }
    }
    server
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())
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
    if matches!(args.peek().map(String::as_str), Some("peer" | "peers")) {
        let json_output = args.next().as_deref() == Some("peers");
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        let peer_token = std::env::var("CLARP_PEER_TOKEN").unwrap_or_default();
        return match clarp_host::admin::peer(args, &token, &peer_token, json_output) {
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
    if args.peek().map(String::as_str) == Some("tool-token") {
        // The token of the agents' tools, from the administrator's in CLARP_HOST_TOKEN: what
        // install-host writes into the tools' own file, so that no script needs the other.
        args.next();
        let token = std::env::var("CLARP_HOST_TOKEN").unwrap_or_default();
        if token.is_empty() || args.next().is_some() {
            eprintln!("usage: CLARP_HOST_TOKEN=... clarp-host tool-token");
            return ExitCode::from(2);
        }
        println!("{}", clarp_host::tool_token(&token));
        return ExitCode::SUCCESS;
    }
    if args.peek().map(String::as_str) == Some("tool") {
        args.next();
        // What the turn was given for its agent's tools; the administrator's, when the
        // administrator calls a tool by hand. The administrator's token put in CLARP_HOST_TOKEN
        // on purpose is the owner's mode, and is said so (`OWNER_MODE_HEADER`); the one a
        // tool finds in the installer's file is not.
        let tools = std::env::var(clarp_host::TOOL_TOKEN_ENV)
            .ok()
            .filter(|token| !token.is_empty());
        let owners = std::env::var("CLARP_HOST_TOKEN")
            .ok()
            .filter(|token| !token.is_empty());
        let owner_mode = tools.is_none() && owners.is_some();
        let token = tools.or(owners).unwrap_or_default();
        let outcome = clarp_host::admin::tool(args, &token, owner_mode);
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
    if let Err(why) = clarp_host::config::name_config_dir(config.config_dir.clone()) {
        eprintln!("clarp-host: {why}");
        return ExitCode::from(2);
    }
    clarp_host::set_lane_limits_default(config.lane_limits_default.as_deref());
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
    let result = runtime.block_on(serve(config));
    // After the HTTP drain, dropping the runtime must not start another wait
    // for blocking requests. The turns live in their own units.
    runtime.shutdown_background();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("clarp-host: {error}");
            ExitCode::FAILURE
        }
    }
}
