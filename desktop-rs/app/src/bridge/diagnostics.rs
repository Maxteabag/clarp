//! Diagnostics: the GUI stall watchdog (`crate::stall_monitor`) and, once a
//! minute, a `memory {...}` line with what the process holds (C++ main's
//! memory log): the kernel's RSS split, live Qt Quick items, the
//! controller's caches, CPU and stalls. The root document creates it next
//! to Main; it starts a second after launch, like the C++ deferred services.
//!
//! `CLARP_STALL_THRESHOLD_MS` (150; 0 disables), `CLARP_STALL_MEMORY_MB`
//! (1536), `CLARP_STALL_LOG` and `CLARP_MEMORY_LOG_SECONDS` (60; 0
//! disables). Screenshot runs keep the watchdog off unless asked: they must
//! not signal the GUI thread mid-test.

use std::pin::Pin;
use std::time::Duration;

use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::QString;

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!(<QtCore/QAbstractEventDispatcher>);
        type QAbstractEventDispatcher;
        type QThread;
        /// The GUI thread's dispatcher (null: the calling thread's).
        #[Self = "QAbstractEventDispatcher"]
        #[cxx_name = "instance"]
        unsafe fn event_dispatcher(thread: *mut QThread) -> *mut QAbstractEventDispatcher;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(QString, last_memory_report, cxx_name = "lastMemoryReport", READ, NOTIFY = last_memory_report_changed)]
        #[qproperty(bool, watching, READ, NOTIFY = watching_changed)]
        type Diagnostics = super::DiagnosticsRust;
    }

    unsafe extern "RustQt" {
        #[qsignal]
        #[cxx_name = "lastMemoryReportChanged"]
        fn last_memory_report_changed(self: Pin<&mut Diagnostics>);
        #[qsignal]
        #[cxx_name = "watchingChanged"]
        fn watching_changed(self: Pin<&mut Diagnostics>);
        /// Time for the minute line: the handler counts the window's items
        /// and hands them to `logMemory` (cxx cannot walk a QList of items).
        /// Forwarded from `QAbstractEventDispatcher::awake()`.
        #[qsignal]
        #[cxx_name = "eventLoopAwake"]
        fn event_loop_awake(self: Pin<&mut Diagnostics>);
        #[qsignal]
        #[cxx_name = "memorySampleRequested"]
        fn memory_sample_requested(self: Pin<&mut Diagnostics>);
        #[qinvokable]
        #[cxx_name = "logMemory"]
        fn log_memory(self: Pin<&mut Diagnostics>, items: i64, text_items: i64);
    }

    impl cxx_qt::Threading for Diagnostics {}
    impl cxx_qt::Initialize for Diagnostics {}
}

#[derive(Default)]
pub struct DiagnosticsRust {
    last_memory_report: QString,
    watching: bool,
    monitor: Option<crate::stall_monitor::StallMonitor>,
    wake_connections: Vec<cxx_qt::QMetaObjectConnectionGuard>,
    cpu: clarp_core::diagnostics::CpuRate,
}

fn env_ms(name: &str, default: i64) -> i64 {
    match std::env::var(name) {
        Ok(value) => value.trim().parse().unwrap_or_else(|_| {
            eprintln!("Diagnostics: {name}={value} is not a number; using {default}");
            default
        }),
        Err(_) => default,
    }
}

impl cxx_qt::Initialize for qobject::Diagnostics {
    fn initialize(self: Pin<&mut Self>) {
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_secs(1), move || {
            if qt.queue(|diagnostics| diagnostics.start()).is_err() {
                eprintln!("Diagnostics: dropped the start; the window is gone");
            }
        });
    }
}

impl qobject::Diagnostics {
    fn start(mut self: Pin<&mut Self>) {
        let screenshot = std::env::var_os("CLARP_SCREENSHOT_PATH").is_some();
        let threshold = env_ms("CLARP_STALL_THRESHOLD_MS", if screenshot { 0 } else { 150 });
        let memory_mb = env_ms("CLARP_STALL_MEMORY_MB", if screenshot { 0 } else { 1536 });
        let log = std::env::var_os("CLARP_STALL_LOG")
            .filter(|path| !path.is_empty())
            .map(std::path::PathBuf::from)
            .or_else(clarp_core::diagnostics::default_stall_log);
        if let Some(log) = log.filter(|_| threshold > 0) {
            let qt = self.qt_thread();
            let monitor = crate::stall_monitor::StallMonitor::start(threshold, memory_mb, log, move |shared| {
                qt.queue(move |_| shared.beat()).is_ok()
            });
            let shared = monitor.shared().clone();
            let woke = self.as_mut().on_event_loop_awake(move |_| shared.awake());
            // SAFETY: the dispatcher lives as long as the GUI thread; the
            // guards disconnect before this object goes.
            let dispatcher = unsafe { qobject::QAbstractEventDispatcher::event_dispatcher(std::ptr::null_mut()) };
            let self_object = (&*self as *const qobject::Diagnostics).cast::<super::quick::ffi::QObject>();
            let forwarded = (!dispatcher.is_null())
                .then(|| unsafe { super::quick::forward_signal(dispatcher.cast(), c"awake()", self_object, c"eventLoopAwake()") })
                .flatten();
            if forwarded.is_none() {
                eprintln!("Diagnostics: no event dispatcher; stalls are measured from heartbeats only");
            }
            self.as_mut().rust_mut().wake_connections.extend(forwarded.into_iter().chain([woke]));
            self.as_mut().rust_mut().monitor = Some(monitor);
            self.as_mut().rust_mut().watching = true;
            self.as_mut().watching_changed();
        } else if threshold > 0 {
            eprintln!("Diagnostics: no stall log path (set CLARP_STALL_LOG or HOME)");
        }
        let seconds = env_ms("CLARP_MEMORY_LOG_SECONDS", 60);
        if seconds > 0 {
            self.schedule_sample(Duration::from_secs(seconds as u64));
        }
    }

    fn schedule_sample(self: Pin<&mut Self>, interval: Duration) {
        let qt = self.qt_thread();
        crate::runtime::after(interval, move || {
            let queued = qt.queue(move |mut diagnostics| {
                diagnostics.as_mut().memory_sample_requested();
                diagnostics.schedule_sample(interval);
            });
            if queued.is_err() {
                eprintln!("Diagnostics: stopped the memory log; the window is gone");
            }
        });
    }

    fn log_memory(mut self: Pin<&mut Self>, items: i64, text_items: i64) {
        use serde_json::{Map, Value, json};
        let mut report = Map::new();
        if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
            for (key, kb) in clarp_core::diagnostics::memory_kb(&status) {
                report.insert(key.into(), json!(kb));
            }
        }
        report.insert("items".into(), json!(items));
        report.insert("textItems".into(), json!(text_items));
        // SAFETY: the GUI thread; the controller outlives the window's diagnostics.
        if let Some(controller) = unsafe { super::controller::window_controller() }
            && let Value::Object(counters) = controller.memory_counters()
        {
            report.extend(counters);
        }
        let cpu = std::fs::read_to_string("/proc/self/stat").ok().and_then(|stat| clarp_core::diagnostics::cpu_ticks(&stat));
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
        let now = chrono::Utc::now().timestamp_millis();
        report.insert("cpuMsPerSec".into(), json!(cpu.map_or(0, |ticks| self.as_mut().rust_mut().cpu.sample(ticks, now, hz))));
        let (stalls, longest) = self.monitor.as_ref().map_or((0, 0), |monitor| monitor.shared().take_counters());
        report.insert("stalls".into(), json!(stalls));
        report.insert("longestStallMs".into(), json!(longest));
        let line = Value::Object(report).to_string();
        eprintln!("memory {line}");
        self.as_mut().rust_mut().last_memory_report = QString::from(&line);
        self.last_memory_report_changed();
    }
}
