//! Capture is local to this Host. Producers never acquire a file or wait for space.
use crate::edge::{ApiError, App};
use axum::{
    Json,
    body::Bytes,
    extract::{MatchedPath, Request, State},
    http::StatusCode,
    middleware::Next,
    response::Response,
};
use clarp_kernel::diagnostics::{self as rules, Settings};
use clarp_ports::{clock::Clock, diagnostics::DiagnosticStore};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU16, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, watch};
pub const CAP: u64 = 256 * 1024 * 1024;
const MAX_RECORD: usize = 32 * 1024;
type Row = (i64, Vec<u8>);
#[derive(Default, Clone, Copy)]
struct LastSeen {
    success: Option<i64>,
    error: Option<i64>,
}
#[derive(Clone, Copy)]
pub enum Subsystem {
    Stt,
    Tts,
    Relay,
}
impl Subsystem {
    fn name(self) -> &'static str {
        match self {
            Self::Stt => "stt",
            Self::Tts => "tts_worker",
            Self::Relay => "relay",
        }
    }
}
pub struct Capture {
    bits: AtomicU16,
    queue: mpsc::Sender<Row>,
    dropped: Arc<AtomicU64>,
    settings_lock: tokio::sync::Mutex<()>,
    resources: watch::Sender<bool>,
    health: Mutex<[LastSeen; 3]>,
}
impl Capture {
    pub fn start(
        mut store: Box<dyn DiagnosticStore>,
        clock: Arc<dyn Clock + Send + Sync>,
        setting: Settings,
        data: PathBuf,
    ) -> Arc<Self> {
        let (queue, mut rx) = mpsc::channel::<Row>(256);
        let (resources, mut mode) = watch::channel(setting.allows("resources"));
        let dropped = Arc::new(AtomicU64::new(0));
        let desk = Arc::new(Self {
            bits: AtomicU16::new(setting.0),
            queue,
            dropped: dropped.clone(),
            settings_lock: Default::default(),
            resources,
            health: Default::default(),
        });
        let timer = clock.clone();
        tokio::spawn(async move {
            loop {
                let now = timer.now_ms();
                let delay = 3_600_000 - now.rem_euclid(3_600_000);
                let mut batch = Vec::new();
                if tokio::time::timeout(
                    Duration::from_millis(delay as u64),
                    rx.recv_many(&mut batch, 64),
                )
                .await
                .is_ok_and(|n| n == 0)
                {
                    break;
                }
                let now = timer.now_ms();
                let Ok((returned, lost)) = tokio::task::spawn_blocking(move || {
                    let lost = store.append(now, &batch).unwrap_or(batch.len());
                    (store, lost)
                })
                .await
                else {
                    break;
                };
                store = returned;
                dropped.fetch_add(lost as u64, Ordering::Relaxed);
            }
        });
        let weak = Arc::downgrade(&desk);
        tokio::spawn(async move {
            loop {
                if mode.wait_for(|on| *on).await.is_err() {
                    break;
                }
                tokio::select! {
                    change=mode.changed()=>{if change.is_err(){break;}},
                    ()=tokio::time::sleep(Duration::from_secs(15))=>{
                        let Some(desk)=weak.upgrade() else{break;};
                        if !desk.settings().allows("resources"){continue;}
                        let dir=data.clone();
                        let at=clock.now_ms();
                        let _=tokio::task::spawn_blocking(move||desk.emit("resources",at,||sample(&dir))).await;
                    }
                }
            }
        });
        desk
    }
    pub(crate) fn settings(&self) -> Settings {
        Settings(self.bits.load(Ordering::Acquire))
    }
    fn emit(&self, category: &'static str, now: i64, make: impl FnOnce() -> Value) -> bool {
        let s = self.settings();
        if !(if category == "requests" {
            s.requests()
        } else {
            s.allows(category)
        }) {
            return false;
        }
        let Ok(slot) = self.queue.try_reserve() else {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return false;
        };
        let value = json!({"category":category,"at":now,"event":make()});
        let bytes = value.to_string().into_bytes();
        if bytes.len() > MAX_RECORD {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return false;
        }
        slot.send((now, bytes));
        true
    }
    pub(crate) fn turn(&self, at:i64,kind:&'static str,agent_id:&str,turn:u64){
        self.emit("agents",at,||json!({"kind":kind,"agent_id":agent_id,"turn":turn}));
    }
    pub(crate) fn relay_connected(&self,at:i64,session:u64){
        self.mark(Subsystem::Relay,true,at);
        self.emit("network",at,||json!({"kind":"relay_connected","session":session}));
    }
    pub(crate) fn relay_lost(&self,at:i64,connected:bool,streams:(usize,usize)){
        self.mark(Subsystem::Relay,false,at);
        self.emit("network",at,||json!({"kind":"relay_lost","connected":connected,"http_count":streams.0,"socket_count":streams.1}));
    }
    pub(crate) fn synthesis(&self,at:i64,ok:bool,bytes:u64,started:Option<Instant>){
        self.mark(Subsystem::Tts,ok,at);
        if let Some(t)=started{
            self.emit("voice",at,||json!({"kind":"synthesis","bytes":bytes,"duration_ms":t.elapsed().as_millis(),"ok":ok}));
        }
    }
    pub fn transcription(&self, at: i64, ok: bool, size: usize, elapsed: Option<Instant>) {
        self.mark(Subsystem::Stt, ok, at);
        if let Some(t) = elapsed {
            self.emit("transcript",at,||json!({"kind":"transcription","size":size,"duration_ms":t.elapsed().as_millis(),"ok":ok}));
        }
    }
    pub fn mark(&self, subsystem: Subsystem, ok: bool, at: i64) {
        let mut clocks = self
            .health
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let entry = &mut clocks[subsystem as usize];
        if ok {
            entry.success = Some(at);
        } else {
            entry.error = Some(at);
        }
    }
    fn subsystems(&self) -> Value {
        let clocks = *self
            .health
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut value = json!({});
        for s in [Subsystem::Stt, Subsystem::Tts, Subsystem::Relay] {
            let e = clocks[s as usize];
            if e.success.is_some() || e.error.is_some() {
                let seconds = |at: i64| at as f64 / 1000.0;
                value[s.name()] = json!({"last_success_at":e.success.map(seconds),"last_error_at":e.error.map(seconds),"last_error":e.error.map(|_|"failed")});
            }
        }
        value
    }
}
fn sample(dir: &Path) -> Value {
    sample_from(dir, Path::new("/proc"))
}
fn sample_from(dir: &Path, procfs: &Path) -> Value {
    let mut v = json!({"kind":"sample"});
    if let Ok(s) = std::fs::read_to_string(procfs.join("self/status")) {
        for (prefix, key) in [
            ("VmHWM:", "process_rss_kb"),
            ("Threads:", "process_threads"),
        ] {
            if let Some(n) = s
                .lines()
                .find_map(|l| l.strip_prefix(prefix))
                .and_then(|s| s.split_whitespace().next())
                .and_then(|s| s.parse::<u64>().ok())
            {
                v[key] = json!(n);
            }
        }
    }
    if let Some(n) = std::fs::read_to_string(procfs.join("loadavg"))
        .ok()
        .and_then(|s| s.split_whitespace().next()?.parse::<f64>().ok())
    {
        v["load_1m"] = json!(n);
    }
    for kind in ["cpu", "memory", "io"] {
        if let Ok(s) = std::fs::read_to_string(procfs.join("pressure").join(kind)) {
            for field in s.lines().next().unwrap_or_default().split_whitespace() {
                if let Some((k, n)) = field.split_once('=')
                    && matches!(k, "avg10" | "avg60")
                    && let Ok(n) = n.parse::<f64>()
                {
                    v[format!("{kind}_{k}")] = json!(n);
                }
            }
        }
    }
    v["state_bytes"] = json!(std::fs::metadata(dir.join("fleet.db")).map_or(0, |m| m.len()));
    use clarp_ports::diagnostics::DiagnosticUsage;
    let usage =
        clarp_store_files::diagnostics::HourFiles::new(dir.join("diagnostics"), CAP).usage();
    if let Ok((_, bytes)) = usage {
        v["telemetry_bytes"] = json!(bytes);
    }
    v
}
pub async fn settings(State(app): State<App>) -> Json<Value> {
    Json(json!({"ok":true,"settings":app.capture.settings().public()}))
}
pub async fn update(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let bad = |msg: String| ApiError::new(StatusCode::BAD_REQUEST, msg);
    let v: Value = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).map_err(|_| bad("bad json".into()))?
    };
    if !v.is_object() {
        return Err(bad("bad json".into()));
    }
    let s = Settings::update(&v).map_err(bad)?;
    let _lock = app.capture.settings_lock.lock().await;
    app.fleet
        .set(vec![(rules::KEY.into(), s.kept().to_string())])
        .await?;
    app.capture.bits.store(s.0, Ordering::Release);
    app.capture.resources.send_if_modified(|old| {
        let new = s.allows("resources");
        let changed = *old != new;
        *old = new;
        changed
    });
    Ok(Json(json!({"ok":true,"settings":s.public()})))
}
pub async fn health(State(app): State<App>) -> Json<Value> {
    let subsystems = app.capture.subsystems();
    let (pending, active) = app.mouth.queue_counts();
    let mut reply = rules::health(
        app.ears.ready(),
        true,
        subsystems,
        pending,
        active,
        &app.config.deployed_version,
    );
    let dropped = app.capture.dropped.load(Ordering::Relaxed);
    if dropped > 0 {
        reply["diagnostic_events_dropped"] = json!(dropped);
    }
    Json(reply)
}
pub type RequestCapture = (Arc<Capture>, Arc<dyn Clock + Send + Sync>);
pub async fn request(
    State((capture, clock)): State<RequestCapture>,
    req: Request,
    next: Next,
) -> Response {
    if !capture.settings().requests() {
        return next.run(req).await;
    }
    let pattern = req
        .extensions()
        .get::<MatchedPath>()
        .map(|p| p.as_str().to_owned())
        .unwrap_or_else(|| "unmatched".into());
    let method = match req.method().as_str() {
        m @ ("GET" | "POST" | "DELETE" | "HEAD" | "PUT" | "PATCH" | "OPTIONS") => m,
        _ => "other",
    }
    .to_owned();
    let start = Instant::now();
    let reply = next.run(req).await;
    capture.emit("requests",clock.now_ms(),||json!({"method":method,"route":pattern,"status":reply.status().as_u16(),"duration_ms":start.elapsed().as_millis()}));
    reply
}
// Stop the size check as soon as the input cannot fit. Do not allocate
// a second copy of a large client string just to decide to discard it.
fn detail_fits(detail: &Value) -> bool {
    struct Budget(usize);
    impl std::io::Write for Budget {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 = self
                .0
                .checked_sub(bytes.len())
                .ok_or_else(|| std::io::Error::other("oversize detail"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    serde_json::to_writer(Budget(MAX_RECORD), detail).is_ok()
}
pub async fn clog(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let s = app.capture.settings();
    if !s.uploads() {
        // Validate the JSON without constructing any strings or event rows.
        let mut de = serde_json::Deserializer::from_slice(&body);
        if body.iter().copied().find(|b| !b.is_ascii_whitespace()) != Some(b'{')
            || <serde::de::IgnoredAny as serde::Deserialize>::deserialize(&mut de).is_err()
            || de.end().is_err()
        {
            return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
        }
        return Ok(Json(json!({"ok":true,"captured":false})));
    }
    let Ok(v @ Value::Object(_)) = serde_json::from_slice::<Value>(&body) else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    let items = v["events"]
        .as_array()
        .map_or_else(|| std::slice::from_ref(&v), Vec::as_slice);
    let mut captured = false;
    for item in items {
        let name = item["event"].as_str().unwrap_or("log");
        let category = rules::client_category(name);
        captured |= app.capture.emit(category, app.clock.now_ms(), || {
            let detail = &item["detail"];
            let detail = if !detail_fits(detail) {
                json!({"truncated":true})
            } else {
                rules::safe_detail(detail)
            };
            json!({"kind":"client_upload","event_id":rules::masked(&json!(name)),"detail":detail})
        });
    }
    Ok(Json(if captured {
        json!({"ok":true})
    } else {
        json!({"ok":true,"captured":false})
    }))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn diagnostic_writer_blockage_preserves_the_queue_bound_and_producer_progress() {
        use clarp_ports::clock::ManualClock;
        struct Blocked {
            entered:Option<tokio::sync::oneshot::Sender<()>>,
            release:std::sync::mpsc::Receiver<()>,
            receipts:mpsc::UnboundedSender<Vec<Value>>,
        }
        impl DiagnosticStore for Blocked {
            fn append(&mut self,_:i64,records:&[Row])->std::io::Result<usize>{
                if let Some(entered)=self.entered.take(){
                    entered.send(()).unwrap();
                    self.release.recv_timeout(Duration::from_secs(5)).unwrap();
                }
                let batch=records.iter().map(|(_,v)|serde_json::from_slice(v).unwrap()).collect();
                self.receipts.send(batch).unwrap();
                Ok(0)
            }
        }
        let (entered,blocked)=tokio::sync::oneshot::channel();
        let (release,unblock)=std::sync::mpsc::channel();
        let (receipts,mut results)=mpsc::unbounded_channel();
        let store=Blocked{entered:Some(entered),release:unblock,receipts};
        let setting=Settings::update(&json!({"enabled":true,"categories":["client"]})).unwrap();
        let capture=Capture::start(Box::new(store),Arc::new(ManualClock::starting_at(1)),setting,PathBuf::new());
        assert!(capture.emit("client",1,||json!({"seq":0})));
        tokio::time::timeout(Duration::from_secs(3),blocked).await.unwrap().unwrap();
        for seq in 1..=256 {assert!(capture.emit("client",1,||json!({"seq":seq})));}
        assert!(!capture.emit("client",1,||panic!("the rejected event may not allocate")));
        assert_eq!(capture.dropped.load(Ordering::Relaxed),1);
        release.send(()).unwrap();
        let mut numbers=Vec::new();
        while numbers.len()<257 {
            let batch=tokio::time::timeout(Duration::from_secs(3),results.recv()).await.unwrap().unwrap();
            assert!(!batch.is_empty() && batch.len()<=64);
            numbers.extend(batch.iter().map(|v|v["event"]["seq"].as_u64().unwrap()));
        }
        assert_eq!(numbers,(0..257).collect::<Vec<_>>());
    }

    #[tokio::test]
    async fn diagnostic_request_records_only_the_route_pattern() {
        use axum::{Router, body::Body, middleware, routing::get};
        use clarp_ports::clock::ManualClock;
        use tower::ServiceExt;
        // A stand-in for the file store receives exactly what the writer was given.
        struct Probe(tokio::sync::mpsc::UnboundedSender<Value>);
        impl DiagnosticStore for Probe {
            fn append(&mut self, _: i64, records: &[Row]) -> std::io::Result<usize> {
                for (_, bytes) in records {
                    self.0.send(serde_json::from_slice(bytes).unwrap()).unwrap();
                }
                Ok(0)
            }
        }
        let (tx, mut records) = mpsc::unbounded_channel();
        let clock: Arc<dyn Clock + Send + Sync> = Arc::new(ManualClock::starting_at(123));
        let capture = Capture::start(
            Box::new(Probe(tx)),
            clock.clone(),
            Settings::update(&json!({"enabled":true,"categories":["database","transcript"]}))
                .unwrap(),
            PathBuf::new(),
        );
        let app = Router::new()
            .route("/agents/{id}", get(|| async { StatusCode::ACCEPTED }))
            .layer(middleware::from_fn_with_state(
                (capture.clone(), clock),
                request,
            ));
        let reply = app
            .oneshot(
                Request::builder()
                    .uri("/agents/private-path?secret=private-query")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(reply.status(), StatusCode::ACCEPTED);
        let record = tokio::time::timeout(Duration::from_secs(3), records.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(record["at"], 123);
        let event = &record["event"];
        assert_eq!(event["route"], "/agents/{id}");
        assert_eq!(event["status"], 202);
        assert_eq!(event["method"], "GET");
        assert!(event["duration_ms"].is_number());
        assert!(!record.to_string().contains("private-"));
        capture.transcription(3000, false, 99, Some(Instant::now()));
        let record = tokio::time::timeout(Duration::from_secs(3), records.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(record["event"]["size"], 99);
        assert_eq!(record["event"]["ok"], false);
        capture.transcription(4000, true, 0, None);
        assert_eq!(
            capture.subsystems()["stt"],
            json!({"last_success_at":4.0,"last_error_at":3.0,"last_error":"failed"})
        );
    }
    #[test]
    fn diagnostic_producers_record_metadata_under_their_own_categories(){
        let (queue,mut read)=mpsc::channel(8);
        let desk=Capture{bits:AtomicU16::new(0),queue,dropped:Default::default(),settings_lock:Default::default(),resources:watch::channel(false).0,health:Default::default()};
        for selected in ["agents","network","voice","database"]{
            desk.bits.store(Settings::update(&json!({"enabled":true,"categories":[selected]})).unwrap().0,Ordering::Release);
            for kind in ["accepted","started","settled"]{desk.turn(1,kind,"fixture-id",7);}
            desk.relay_connected(2,8);
            desk.relay_lost(3,true,(11,12));
            desk.synthesis(4,false,17,Some(Instant::now()));
            let mut events=Vec::new();
            while let Ok((_,bytes))=read.try_recv(){
                let record:Value=serde_json::from_slice(&bytes).unwrap();
                assert_eq!(record["category"],selected);
                events.push(record["event"].clone());
            }
            match selected{
                "agents"=>assert_eq!(events,["accepted","started","settled"].map(|kind|json!({"kind":kind,"agent_id":"fixture-id","turn":7}))),
                "network"=>assert_eq!(events,[json!({"kind":"relay_connected","session":8}),json!({"kind":"relay_lost","connected":true,"http_count":11,"socket_count":12})]),
                "voice"=>{assert_eq!(events.len(),1);assert!(events[0]["duration_ms"].is_number());assert_eq!(events[0]["bytes"],17);assert_eq!(events[0]["ok"],false);},
                _=>assert!(events.is_empty()),
            }
        }
    }
    #[test]
    fn diagnostic_resource_sample_uses_only_the_named_measurements() {
        let root = std::env::temp_dir().join(format!("clarp-diag-sample-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let procfs = root.join("proc");
        let data = root.join("data");
        for p in [procfs.join("self"), procfs.join("pressure"), data.clone()] {
            std::fs::create_dir_all(p).unwrap();
        }
        std::fs::write(
            procfs.join("self/status"),
            "Name: private-process\nVmHWM: 123 kB\nThreads: 7\n",
        )
        .unwrap();
        std::fs::write(procfs.join("loadavg"), "0.25 0.3 0.5 1/100 999\n").unwrap();
        for kind in ["cpu", "io", "memory"] {
            std::fs::write(
                procfs.join("pressure").join(kind),
                "some avg10=1.0 avg60=2.0 total=999\n",
            )
            .unwrap();
        }
        std::fs::write(data.join("fleet.db"), b"synthetic").unwrap();
        let v = sample_from(&data, &procfs);
        assert_eq!(v["process_rss_kb"], 123);
        assert_eq!(v["process_threads"], 7);
        assert_eq!(v["load_1m"], 0.25);
        assert_eq!(v["state_bytes"], 9);
        assert_eq!(v["telemetry_bytes"], 0);
        for kind in ["cpu", "io", "memory"] {
            assert_eq!(v[format!("{kind}_avg10")], 1.0);
            assert_eq!(v[format!("{kind}_avg60")], 2.0);
        }
        assert_eq!(v.as_object().unwrap().len(), 12);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn diagnostic_queue_drops_new_events_without_building_them() {
        let (queue, mut read) = mpsc::channel(1);
        let capture = Capture {
            bits: AtomicU16::new(0),
            queue,
            dropped: Default::default(),
            settings_lock: Default::default(),
            resources: watch::channel(false).0,
            health: Default::default(),
        };
        assert!(!capture.emit("agents", 0, || panic!("allocated while disabled")));
        capture.bits.store(
            Settings::update(&json!({"enabled":true,"categories":["agents"]}))
                .unwrap()
                .0,
            Ordering::Release,
        );
        assert!(capture.emit("agents", 0, || json!({"kind":"first"})));
        for _ in 0..128 {
            assert!(!capture.emit("agents", 1, || panic!("allocated while full")));
        }
        assert_eq!(capture.dropped.load(Ordering::Relaxed), 128);
        let (_, first) = read.try_recv().unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&first).unwrap()["event"]["kind"],
            "first"
        );
        assert!(!capture.emit("agents", 2, || Value::String("x".repeat(MAX_RECORD))));
        assert!(read.try_recv().is_err());
    }
}
