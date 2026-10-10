//! Catches the moments the desktop feels laggy (C++ `StallMonitor`): a
//! watchdog thread expects the GUI thread to answer a heartbeat. When it
//! misses for longer than the threshold, the watchdog interrupts the GUI
//! thread, captures its call stack right there, and appends it to the stall
//! log; when the GUI thread comes back it logs how long the stall lasted.
//! Resident memory crossing a mark (and every further 512 MB) captures the
//! stack too, so a runaway allocation is caught before the launcher's
//! memory limit kills the process. Unlike the C++ build, frames are named
//! from debug info after the capture, not only from exported symbols; the
//! release keeps its symbol table (desktop.yml strips debug info only), and
//! each frame also says its file and offset, so one left unnamed can be
//! named later from the same build.

use std::ffi::c_void;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use clarp_core::diagnostics::{MemoryWatch, StallDetector, StallEvent};

const MAX_FRAMES: usize = 64;
const ROTATE_BYTES: u64 = 4 * 1024 * 1024;

unsafe extern "C" {
    // <execinfo.h> (glibc, libSystem); async-signal-safe once warmed up.
    fn backtrace(buffer: *mut *mut c_void, size: i32) -> i32;
}

// Filled by the signal handler on the GUI thread, read by the watchdog. A
// signal handler can only reach process-wide state.
static mut FRAMES: [*mut c_void; MAX_FRAMES] = [std::ptr::null_mut(); MAX_FRAMES];
static FRAME_COUNT: AtomicI32 = AtomicI32::new(0);
static CAPTURED: AtomicBool = AtomicBool::new(false);

extern "C" fn capture_stack(_signal: i32) {
    // SAFETY: only this handler writes FRAMES, and the watchdog reads it
    // only after CAPTURED is set.
    let count = unsafe { backtrace(std::ptr::addr_of_mut!(FRAMES).cast(), MAX_FRAMES as i32) };
    FRAME_COUNT.store(count, Ordering::SeqCst);
    CAPTURED.store(true, Ordering::SeqCst);
}

#[cfg(target_os = "linux")]
fn stack_signal() -> i32 {
    libc::SIGRTMIN() + 7
}

/// macOS has no real-time signals; nothing else here uses SIGUSR2.
#[cfg(not(target_os = "linux"))]
fn stack_signal() -> i32 {
    libc::SIGUSR2
}

/// What the watchdog and the minute line share.
pub struct Shared {
    started: Instant,
    last_beat_ms: AtomicI64,
    last_awake_ms: AtomicI64,
    running: AtomicBool,
    detector: Mutex<StallDetector>,
}

impl Shared {
    fn now_ms(&self) -> i64 {
        self.started.elapsed().as_millis() as i64
    }

    /// The GUI thread ran: called from it.
    pub fn beat(&self) {
        self.last_beat_ms.store(self.now_ms(), Ordering::SeqCst);
    }

    /// The GUI event loop woke to work (`QAbstractEventDispatcher::awake`),
    /// on the GUI thread: a block is measured from here.
    pub fn awake(&self) {
        self.last_awake_ms.store(self.now_ms(), Ordering::SeqCst);
    }

    pub fn running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    /// Stall count and the longest stall since the last call.
    pub fn take_counters(&self) -> (u32, i64) {
        match self.detector.lock() {
            Ok(mut detector) => (detector.stalls(), detector.take_longest_ms()),
            Err(error) => {
                eprintln!("StallMonitor: counters unavailable: {error}");
                (0, 0)
            }
        }
    }
}

pub struct StallMonitor {
    shared: Arc<Shared>,
    watchdog: Option<std::thread::JoinHandle<()>>,
}

impl StallMonitor {
    /// Starts watching the calling thread, which must be the GUI thread.
    /// `threshold_ms <= 0` watches nothing but still counts (zero stalls).
    /// `request_beat` asks the GUI thread to call `Shared::beat`; false
    /// means the GUI side is gone.
    pub fn start(threshold_ms: i64, memory_mb: i64, log: PathBuf, request_beat: impl Fn(Arc<Shared>) -> bool + Send + 'static) -> Self {
        let shared = Arc::new(Shared {
            started: Instant::now(),
            last_beat_ms: AtomicI64::new(0),
            last_awake_ms: AtomicI64::new(0),
            running: AtomicBool::new(threshold_ms > 0),
            detector: Mutex::new(StallDetector::new(threshold_ms)),
        });
        if threshold_ms <= 0 {
            return Self { shared, watchdog: None };
        }
        if let Some(parent) = log.parent()
            && let Err(error) = std::fs::create_dir_all(parent)
        {
            eprintln!("StallMonitor: cannot create {}: {error}", parent.display());
        }
        // Warm backtrace() so its first call (which loads libgcc) never
        // happens inside the signal handler.
        let mut warm = [std::ptr::null_mut(); 4];
        unsafe { backtrace(warm.as_mut_ptr(), warm.len() as i32) };
        // SAFETY: installs a handler that only calls backtrace and stores atomics.
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = capture_stack as *const () as usize;
            libc::sigemptyset(&mut action.sa_mask);
            action.sa_flags = libc::SA_RESTART;
            if libc::sigaction(stack_signal(), &action, std::ptr::null_mut()) != 0 {
                eprintln!("StallMonitor: cannot install the stack handler: {}", std::io::Error::last_os_error());
            }
        }
        let gui_thread = unsafe { libc::pthread_self() };
        let watched = shared.clone();
        let watchdog = std::thread::Builder::new()
            .name("stall-watchdog".into())
            .spawn(move || watch(watched, threshold_ms, memory_mb, log, gui_thread, request_beat));
        match watchdog {
            Ok(handle) => Self { shared, watchdog: Some(handle) },
            Err(error) => {
                eprintln!("StallMonitor: cannot start the watchdog: {error}");
                Self { shared, watchdog: None }
            }
        }
    }

    pub fn shared(&self) -> &Arc<Shared> {
        &self.shared
    }
}

impl Drop for StallMonitor {
    fn drop(&mut self) {
        self.shared.running.store(false, Ordering::SeqCst);
        if let Some(watchdog) = self.watchdog.take()
            && watchdog.join().is_err()
        {
            eprintln!("StallMonitor: the watchdog panicked");
        }
    }
}

fn watch(shared: Arc<Shared>, threshold_ms: i64, memory_mb: i64, log: PathBuf, gui_thread: libc::pthread_t, request_beat: impl Fn(Arc<Shared>) -> bool) {
    let poll = clarp_core::diagnostics::poll_interval_ms(threshold_ms);
    let memory_ticks = (500 / poll).max(1);
    let mut memory = MemoryWatch::new(memory_mb);
    let mut tick = 0;
    shared.beat();
    while shared.running() {
        if !request_beat(shared.clone()) {
            return;
        }
        std::thread::sleep(Duration::from_millis(poll as u64));
        tick += 1;
        if memory_mb > 0 && tick % memory_ticks == 0 {
            let resident = std::fs::read_to_string("/proc/self/statm")
                .map(|statm| clarp_core::diagnostics::resident_mb(&statm, unsafe { libc::sysconf(libc::_SC_PAGESIZE) }))
                .unwrap_or(0);
            if memory.crossed(resident) {
                let frames = capture_gui_stack(gui_thread);
                write_capture(&log, "memory", &format!("rss={resident}MB"), &frames);
            }
        }
        let event = match shared.detector.lock() {
            Ok(mut detector) => detector.tick(shared.last_beat_ms.load(Ordering::SeqCst), shared.last_awake_ms.load(Ordering::SeqCst), shared.now_ms()),
            Err(error) => {
                eprintln!("StallMonitor: stopped: {error}");
                return;
            }
        };
        match event {
            Some(StallEvent::Began { gap_ms }) => {
                let frames = capture_gui_stack(gui_thread);
                write_capture(&log, "stall", &format!("blocked>={gap_ms}ms"), &frames);
            }
            Some(StallEvent::Ended { total_ms }) => append(&log, &format!("-- stall ended after {total_ms} ms\n")),
            None => {}
        }
    }
}

fn capture_gui_stack(gui_thread: libc::pthread_t) -> Vec<usize> {
    CAPTURED.store(false, Ordering::SeqCst);
    // SAFETY: the GUI thread lives as long as the monitor.
    if unsafe { libc::pthread_kill(gui_thread, stack_signal()) } != 0 {
        return Vec::new();
    }
    for _ in 0..20 {
        if CAPTURED.load(Ordering::SeqCst) {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    if !CAPTURED.load(Ordering::SeqCst) {
        return Vec::new();
    }
    let count = FRAME_COUNT.load(Ordering::SeqCst).clamp(0, MAX_FRAMES as i32) as usize;
    // SAFETY: the handler finished writing before setting CAPTURED.
    let frames = unsafe { *std::ptr::addr_of!(FRAMES) };
    frames[..count].iter().map(|frame| *frame as usize).collect()
}

fn symbolize(frames: &[usize]) -> String {
    let mut text = String::new();
    for (index, address) in frames.iter().enumerate() {
        let mut named = false;
        backtrace::resolve(*address as *mut c_void, |symbol| {
            if named {
                return;
            }
            named = true;
            let name = symbol.name().map_or_else(|| "??".to_owned(), |name| format!("{name:#}"));
            let place = match (symbol.filename(), symbol.lineno()) {
                (Some(file), Some(line)) => format!(" ({}:{line})", file.display()),
                _ => String::new(),
            };
            text += &format!("#{index:<2} {address:#x} {name}{place}{}\n", module_offset(*address));
        });
        if !named {
            text += &format!("#{index:<2} {address:#x}{}\n", module_offset(*address));
        }
    }
    // Parsed debug info is large; a stall is rare, so do not keep it.
    backtrace::clear_symbol_cache();
    text
}

/// " [clarp-slint+0x1234]": the frame's file and its offset in it, which
/// stays the same from run to run (unlike the address), so a frame the
/// stripped release names "??" can be named later from the same build.
fn module_offset(address: usize) -> String {
    // SAFETY: dladdr only reads the loader's tables and fills the zeroed
    // struct it is given; the name it returns lives as long as the module.
    let mut info: libc::Dl_info = unsafe { std::mem::zeroed() };
    if unsafe { libc::dladdr(address as *const c_void, &mut info) } == 0 || info.dli_fname.is_null() {
        return String::new();
    }
    let file = unsafe { std::ffi::CStr::from_ptr(info.dli_fname) }.to_string_lossy();
    let name = Path::new(file.as_ref()).file_name().map_or_else(|| file.to_string(), |n| n.to_string_lossy().into_owned());
    format!(" [{name}+{:#x}]", address.wrapping_sub(info.dli_fbase as usize))
}

fn write_capture(log: &Path, reason: &str, measure: &str, frames: &[usize]) {
    rotate(log);
    let header = format!(
        "== {reason} at {} pid={} {measure} build={} exe={}\n",
        chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%.3f"),
        std::process::id(),
        env!("CARGO_PKG_VERSION"),
        std::env::current_exe().map_or_else(|e| format!("({e})"), |p| p.display().to_string())
    );
    let stack = if frames.is_empty() { "(stack not captured)\n".to_owned() } else { symbolize(frames) };
    append(log, &(header + &stack));
    eprintln!("clarp {reason}: {measure}, GUI stack in {}", log.display());
}

fn rotate(log: &Path) {
    if std::fs::metadata(log).is_ok_and(|m| m.len() > ROTATE_BYTES) {
        let old = log.with_extension("log.1");
        if let Err(error) = std::fs::rename(log, &old) {
            eprintln!("StallMonitor: cannot rotate {}: {error}", log.display());
        }
    }
}

fn append(log: &Path, text: &str) {
    use std::os::unix::fs::OpenOptionsExt;
    let written = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(log)
        .and_then(|mut file| file.write_all(text.as_bytes()));
    if let Err(error) = written {
        eprintln!("StallMonitor: cannot write {}: {error}", log.display());
    }
}

#[cfg(test)]
mod tests {
    use super::{module_offset, symbolize};

    #[test]
    fn a_frame_is_named_and_placed_in_its_file() {
        let address = symbolize as usize;
        let text = symbolize(&[address]);
        assert!(text.contains("symbolize"), "{text}");
        let offset = module_offset(address);
        assert!(offset.starts_with(" [") && offset.contains("+0x") && offset.ends_with(']'), "{offset}");
        assert!(text.contains(&offset), "{text}");
    }
}
