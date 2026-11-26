//! In-app video through libmpv, loaded at run time: the app starts and
//! plays audio without it, and says what is missing when a video can't play.
//!
//! mpv decodes, keeps sound and picture in step and plays the sound; its
//! software renderer draws each picture into memory, which the window shows
//! as an image. mpv never sees the Host's address or credential: it reads
//! `clarp-media://<n>`, a stream this module serves from the Host with the
//! app's own client (`ApiClient::media_range`).

use std::collections::HashMap;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

use clarp_net::ApiClient;

const FORMAT_FLAG: c_int = 3;
const FORMAT_DOUBLE: c_int = 5;
const EVENT_SHUTDOWN: c_int = 1;
const EVENT_END_FILE: c_int = 7;
const EVENT_FILE_LOADED: c_int = 8;
const EVENT_PROPERTY_CHANGE: c_int = 22;
const END_FILE_ERROR: c_int = 4;
const RENDER_PARAM_INVALID: c_int = 0;
const RENDER_PARAM_API_TYPE: c_int = 1;
const RENDER_PARAM_SW_SIZE: c_int = 17;
const RENDER_PARAM_SW_FORMAT: c_int = 18;
const RENDER_PARAM_SW_STRIDE: c_int = 19;
const RENDER_PARAM_SW_POINTER: c_int = 20;
const RENDER_UPDATE_FRAME: u64 = 1;
const ERROR_LOADING_FAILED: c_int = -13;
const ERROR_UNSUPPORTED: c_int = -18;
const ERROR_GENERIC: c_int = -20;

/// The protocol mpv reads the Host's media through.
const PROTOCOL: &str = "clarp-media";
/// How much one read asks the Host for.
const CHUNK: u64 = 1 << 20;
/// The largest picture drawn (4K); a bigger surface is filled by scaling.
const MAX_PIXELS: u32 = 3840 * 2160;

#[repr(C)]
struct RawEvent {
    event_id: c_int,
    error: c_int,
    reply_userdata: u64,
    data: *mut c_void,
}

#[repr(C)]
struct RawProperty {
    name: *const c_char,
    format: c_int,
    data: *mut c_void,
}

#[repr(C)]
struct RawEndFile {
    reason: c_int,
    error: c_int,
}

#[repr(C)]
struct RenderParam {
    kind: c_int,
    data: *mut c_void,
}

type ReadFn = unsafe extern "C" fn(*mut c_void, *mut c_char, u64) -> i64;
type SeekFn = unsafe extern "C" fn(*mut c_void, i64) -> i64;
type SizeFn = unsafe extern "C" fn(*mut c_void) -> i64;
type CloseFn = unsafe extern "C" fn(*mut c_void);
type CancelFn = unsafe extern "C" fn(*mut c_void);
type OpenFn = unsafe extern "C" fn(*mut c_void, *mut c_char, *mut StreamInfo) -> c_int;
type UpdateFn = unsafe extern "C" fn(*mut c_void);

#[repr(C)]
struct StreamInfo {
    cookie: *mut c_void,
    read_fn: Option<ReadFn>,
    seek_fn: Option<SeekFn>,
    size_fn: Option<SizeFn>,
    close_fn: Option<CloseFn>,
    cancel_fn: Option<CancelFn>,
}

/// libmpv's functions this uses.
struct Lib {
    create: unsafe extern "C" fn() -> *mut c_void,
    initialize: unsafe extern "C" fn(*mut c_void) -> c_int,
    set_option_string: unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char) -> c_int,
    // The window's thread only uses the async calls: they queue and return,
    // so a busy player never holds up drawing.
    set_property_async: unsafe extern "C" fn(*mut c_void, u64, *const c_char, c_int, *mut c_void) -> c_int,
    command_async: unsafe extern "C" fn(*mut c_void, u64, *mut *const c_char) -> c_int,
    observe_property: unsafe extern "C" fn(*mut c_void, u64, *const c_char, c_int) -> c_int,
    wait_event: unsafe extern "C" fn(*mut c_void, f64) -> *mut RawEvent,
    wakeup: unsafe extern "C" fn(*mut c_void),
    terminate_destroy: unsafe extern "C" fn(*mut c_void),
    error_string: unsafe extern "C" fn(c_int) -> *const c_char,
    stream_cb_add_ro: unsafe extern "C" fn(*mut c_void, *const c_char, *mut c_void, OpenFn) -> c_int,
    render_create: unsafe extern "C" fn(*mut *mut c_void, *mut c_void, *mut RenderParam) -> c_int,
    render_set_update_callback: unsafe extern "C" fn(*mut c_void, Option<UpdateFn>, *mut c_void),
    render_update: unsafe extern "C" fn(*mut c_void) -> u64,
    render: unsafe extern "C" fn(*mut c_void, *mut RenderParam) -> c_int,
    render_free: unsafe extern "C" fn(*mut c_void),
}

/// Where libmpv is looked for: the system's library path, and on macOS
/// beside the app and in Homebrew.
fn candidates() -> Vec<String> {
    if cfg!(target_os = "macos") {
        let mut found = Vec::new();
        if let Ok(exe) = std::env::current_exe()
            && let Some(dir) = exe.parent()
        {
            found.push(dir.join("../Frameworks/libmpv.2.dylib").to_string_lossy().into_owned());
        }
        for dir in ["/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib"] {
            found.push(format!("{dir}/libmpv.2.dylib"));
        }
        found.push("libmpv.2.dylib".into());
        found
    } else {
        vec!["libmpv.so.2".into(), "libmpv.so".into()]
    }
}

/// The reason video can't play here, or None when it can.
pub fn unavailable() -> Option<String> {
    lib().err()
}

fn lib() -> Result<&'static Lib, String> {
    static LIB: OnceLock<Result<Lib, String>> = OnceLock::new();
    LIB.get_or_init(load).as_ref().map_err(Clone::clone)
}

fn load() -> Result<Lib, String> {
    if std::env::var_os("CLARP_NO_LIBMPV").is_some() {
        return Err(missing());
    }
    let handle = candidates().into_iter().find_map(|name| {
        let name = CString::new(name).ok()?;
        // SAFETY: dlopen with a valid NUL-terminated name; the library stays
        // loaded for the life of the process (never dlclosed).
        let handle = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOW | libc::RTLD_LOCAL) };
        (!handle.is_null()).then_some(handle)
    });
    let Some(handle) = handle else { return Err(missing()) };
    let symbol = |name: &str| -> Result<*mut c_void, String> {
        let c = CString::new(name).expect("symbol name");
        // SAFETY: dlsym on a handle dlopen returned.
        let found = unsafe { libc::dlsym(handle, c.as_ptr()) };
        if found.is_null() { Err(format!("Video needs a newer mpv (libmpv lacks {name})")) } else { Ok(found) }
    };
    macro_rules! sym {
        ($name:literal) => {
            // SAFETY: the symbol is libmpv's, with the C signature declared
            // in `Lib` (client.h, render.h and stream_cb.h of API 2.x).
            unsafe { std::mem::transmute::<*mut c_void, _>(symbol($name)?) }
        };
    }
    // SAFETY: as for sym!.
    let version: unsafe extern "C" fn() -> std::ffi::c_ulong = unsafe { std::mem::transmute::<*mut c_void, _>(symbol("mpv_client_api_version")?) };
    // SAFETY: a plain getter.
    let version = unsafe { version() };
    if version >> 16 != 2 {
        return Err(format!("Video needs libmpv 2 (found API {}.{})", version >> 16, version & 0xffff));
    }
    Ok(Lib {
        create: sym!("mpv_create"),
        initialize: sym!("mpv_initialize"),
        set_option_string: sym!("mpv_set_option_string"),
        set_property_async: sym!("mpv_set_property_async"),
        command_async: sym!("mpv_command_async"),
        observe_property: sym!("mpv_observe_property"),
        wait_event: sym!("mpv_wait_event"),
        wakeup: sym!("mpv_wakeup"),
        terminate_destroy: sym!("mpv_terminate_destroy"),
        error_string: sym!("mpv_error_string"),
        stream_cb_add_ro: sym!("mpv_stream_cb_add_ro"),
        render_create: sym!("mpv_render_context_create"),
        render_set_update_callback: sym!("mpv_render_context_set_update_callback"),
        render_update: sym!("mpv_render_context_update"),
        render: sym!("mpv_render_context_render"),
        render_free: sym!("mpv_render_context_free"),
    })
}

fn missing() -> String {
    if cfg!(target_os = "macos") { "Video needs mpv: brew install mpv".into() } else { "Video needs mpv (libmpv) installed".into() }
}

fn error_text(lib: &Lib, code: c_int) -> String {
    // SAFETY: mpv_error_string returns a static string for any code.
    unsafe { CStr::from_ptr((lib.error_string)(code)) }.to_string_lossy().into_owned()
}

/// How the video is going, as the window shows it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct State {
    pub loaded: bool,
    pub paused: bool,
    pub buffering: bool,
    pub ended: bool,
    pub position: f64,
    pub duration: f64,
    pub volume: f64,
    pub muted: bool,
    pub failed: Option<String>,
}

/// What the player hands the window, from its own threads.
pub enum Update {
    State(State),
    /// A drawn picture: width, height and RGBA pixels.
    Frame(slint::SharedPixelBuffer<slint::Rgba8Pixel>),
}

/// The Host media a `clarp-media://<n>` stream reads.
struct Source {
    api: ApiClient,
    path: String,
    runtime: tokio::runtime::Handle,
}

fn sources() -> &'static Mutex<HashMap<u64, Source>> {
    static SOURCES: OnceLock<Mutex<HashMap<u64, Source>>> = OnceLock::new();
    SOURCES.get_or_init(Mutex::default)
}

/// One open stream (mpv's cookie).
struct Stream {
    api: ApiClient,
    path: String,
    runtime: tokio::runtime::Handle,
    position: u64,
    total: Option<u64>,
    /// Bytes read ahead, and where they begin in the file.
    buffer: Vec<u8>,
    buffer_start: u64,
    cancelled: Arc<AtomicBool>,
    cancel: Arc<tokio::sync::Notify>,
}

impl Stream {
    fn fetch(&mut self, start: u64) -> Result<(), String> {
        let api = self.api.clone();
        let path = self.path.clone();
        let cancel = self.cancel.clone();
        let cancelled = self.cancelled.clone();
        let reply = self.runtime.block_on(async move {
            let read = std::pin::pin!(api.media_range(&path, start, CHUNK));
            let stop = std::pin::pin!(async {
                while !cancelled.load(Ordering::SeqCst) {
                    cancel.notified().await;
                }
            });
            match futures_util::future::select(read, stop).await {
                futures_util::future::Either::Left((reply, _)) => reply,
                futures_util::future::Either::Right(_) => Err("cancelled".into()),
            }
        })?;
        if reply.total.is_some() {
            self.total = reply.total;
        }
        self.buffer_start = reply.start;
        self.buffer = reply.bytes;
        Ok(())
    }

    fn read(&mut self, out: &mut [u8]) -> Result<usize, String> {
        let end = self.buffer_start + self.buffer.len() as u64;
        if self.position < self.buffer_start || self.position >= end {
            if self.total.is_some_and(|t| self.position >= t) {
                return Ok(0);
            }
            self.fetch(self.position)?;
        }
        let end = self.buffer_start + self.buffer.len() as u64;
        if self.position < self.buffer_start || self.position >= end {
            return Ok(0);
        }
        let from = (self.position - self.buffer_start) as usize;
        let n = out.len().min(self.buffer.len() - from);
        out[..n].copy_from_slice(&self.buffer[from..from + n]);
        self.position += n as u64;
        Ok(n)
    }
}

unsafe extern "C" fn stream_open(_user: *mut c_void, uri: *mut c_char, info: *mut StreamInfo) -> c_int {
    // SAFETY: mpv passes a NUL-terminated URI and a writable info struct.
    let uri = unsafe { CStr::from_ptr(uri) }.to_string_lossy().into_owned();
    let Some(id) = uri.strip_prefix(&format!("{PROTOCOL}://")).and_then(|n| n.parse::<u64>().ok()) else { return ERROR_LOADING_FAILED };
    let Some((api, path, runtime)) = sources().lock().expect("sources").get(&id).map(|s| (s.api.clone(), s.path.clone(), s.runtime.clone())) else {
        return ERROR_LOADING_FAILED;
    };
    let mut stream = Box::new(Stream {
        api,
        path,
        runtime,
        position: 0,
        total: None,
        buffer: Vec::new(),
        buffer_start: 0,
        cancelled: Arc::default(),
        cancel: Arc::default(),
    });
    // The first part tells the file's length (and whether it is there).
    if let Err(error) = stream.fetch(0) {
        eprintln!("clarp-slint video: couldn't open the media: {error}");
        return ERROR_LOADING_FAILED;
    }
    // SAFETY: as above.
    let info = unsafe { &mut *info };
    info.cookie = Box::into_raw(stream).cast();
    info.read_fn = Some(stream_read);
    info.seek_fn = Some(stream_seek);
    info.size_fn = Some(stream_size);
    info.close_fn = Some(stream_close);
    info.cancel_fn = Some(stream_cancel);
    0
}

unsafe extern "C" fn stream_read(cookie: *mut c_void, buf: *mut c_char, n: u64) -> i64 {
    // SAFETY: the cookie is the Stream stream_open boxed; mpv calls a
    // stream's functions from one thread at a time; buf holds n bytes.
    let stream = unsafe { &mut *cookie.cast::<Stream>() };
    let out = unsafe { std::slice::from_raw_parts_mut(buf.cast::<u8>(), n as usize) };
    match stream.read(out) {
        Ok(n) => n as i64,
        Err(error) => {
            if error != "cancelled" {
                eprintln!("clarp-slint video: a read failed: {error}");
            }
            -1
        }
    }
}

unsafe extern "C" fn stream_seek(cookie: *mut c_void, offset: i64) -> i64 {
    // SAFETY: as in stream_read.
    let stream = unsafe { &mut *cookie.cast::<Stream>() };
    if offset < 0 {
        return i64::from(ERROR_GENERIC);
    }
    stream.position = offset as u64;
    offset
}

unsafe extern "C" fn stream_size(cookie: *mut c_void) -> i64 {
    // SAFETY: as in stream_read.
    let stream = unsafe { &*cookie.cast::<Stream>() };
    stream.total.map(|t| t as i64).unwrap_or(i64::from(ERROR_UNSUPPORTED))
}

unsafe extern "C" fn stream_close(cookie: *mut c_void) {
    // SAFETY: the Box stream_open made; mpv closes a stream once.
    drop(unsafe { Box::from_raw(cookie.cast::<Stream>()) });
}

unsafe extern "C" fn stream_cancel(cookie: *mut c_void) {
    // SAFETY: cancel may come from another thread: it touches only the
    // stream's shared flag and notifier.
    let stream = unsafe { &*cookie.cast::<Stream>() };
    stream.cancelled.store(true, Ordering::SeqCst);
    stream.cancel.notify_waiters();
}

/// Raw pointers handed between threads; mpv's client and render APIs are
/// thread-safe within the rules this module keeps (one waiter for events,
/// one renderer at a time).
#[derive(Clone, Copy)]
struct Ptr(*mut c_void);
// SAFETY: see above.
unsafe impl Send for Ptr {}
unsafe impl Sync for Ptr {}

/// Wakes the render thread: a new picture, a new size, or stop.
#[derive(Default)]
struct Wake {
    pending: Mutex<bool>,
    ready: Condvar,
}

impl Wake {
    fn ring(&self) {
        *self.pending.lock().expect("wake") = true;
        self.ready.notify_one();
    }
}

unsafe extern "C" fn render_wanted(data: *mut c_void) {
    // SAFETY: data is the Wake the player keeps alive until the render
    // context is freed.
    unsafe { &*data.cast::<Wake>() }.ring();
}

/// One video being played. Dropping it stops the sound at once and tears
/// mpv down on its own thread.
pub struct Player {
    mpv: Ptr,
    stopping: Arc<AtomicBool>,
    /// Held while the handle may still be used from here after `stopping`
    /// is set; the event thread takes it before destroying mpv.
    alive: Arc<Mutex<()>>,
    wake: Arc<Wake>,
    width: Arc<AtomicU32>,
    height: Arc<AtomicU32>,
}

impl Player {
    /// Opens the Host media at `path`, paused on its first picture unless
    /// `autoplay`. `deliver` runs on mpv's threads: it must hand updates to
    /// the UI thread itself.
    pub fn open(api: ApiClient, runtime: tokio::runtime::Handle, path: &str, autoplay: bool, deliver: impl Fn(Update) + Send + Sync + 'static) -> Result<Self, String> {
        let lib = lib()?;
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let source = NEXT.fetch_add(1, Ordering::SeqCst);
        sources().lock().expect("sources").insert(source, Source { api, path: path.to_owned(), runtime });
        // SAFETY: mpv_create has no preconditions; null means out of memory.
        let mpv = unsafe { (lib.create)() };
        if mpv.is_null() {
            sources().lock().expect("sources").remove(&source);
            return Err("Couldn't start the video player".into());
        }
        let set = |name: &str, value: &str| {
            let (n, v) = (CString::new(name).expect("option"), CString::new(value).expect("value"));
            // SAFETY: a fresh, not yet initialized handle and C strings.
            unsafe { (lib.set_option_string)(mpv, n.as_ptr(), v.as_ptr()) }
        };
        // Nothing of the user's mpv setup, no window, keys, scripts or
        // terminal output: this mpv is a decoder inside the app.
        for (name, value) in [
            ("config", "no"),
            ("terminal", "no"),
            ("msg-level", "all=no"),
            ("vo", "libmpv"),
            ("hwdec", "no"),
            ("input-default-bindings", "no"),
            ("input-vo-keyboard", "no"),
            ("osc", "no"),
            ("load-scripts", "no"),
            ("ytdl", "no"),
            ("keep-open", "yes"),
            ("idle", "yes"),
            ("audio-client-name", "Clarp"),
            // An audio file's cover is shown by the app, not drawn as video.
            ("audio-display", "no"),
            ("pause", if autoplay { "no" } else { "yes" }),
        ] {
            set(name, value);
        }
        // Checks: no sound device (CLARP_AUDIO_OUTPUT=null), or the sound
        // written to a file to measure.
        if let Some(file) = std::env::var_os("CLARP_TEST_VIDEO_SOUND_FILE") {
            set("ao", "pcm");
            set("ao-pcm-file", &file.to_string_lossy());
        } else if std::env::var("CLARP_AUDIO_OUTPUT").is_ok_and(|o| o == "null") {
            set("ao", "null");
        }
        let fail = |what: &str, code: c_int| {
            // SAFETY: the handle is ours and used by nothing else yet.
            unsafe { (lib.terminate_destroy)(mpv) };
            sources().lock().expect("sources").remove(&source);
            format!("{what}: {}", error_text(lib, code))
        };
        // SAFETY: the handle is configured and not yet initialized.
        let code = unsafe { (lib.initialize)(mpv) };
        if code < 0 {
            return Err(fail("Couldn't start the video player", code));
        }
        let protocol = CString::new(PROTOCOL).expect("protocol");
        // SAFETY: an initialized handle; stream_open has the required signature.
        let code = unsafe { (lib.stream_cb_add_ro)(mpv, protocol.as_ptr(), std::ptr::null_mut(), stream_open) };
        if code < 0 {
            return Err(fail("Couldn't start the video player", code));
        }
        let mut api_type = *b"sw\0";
        let mut params = [
            RenderParam { kind: RENDER_PARAM_API_TYPE, data: api_type.as_mut_ptr().cast() },
            RenderParam { kind: RENDER_PARAM_INVALID, data: std::ptr::null_mut() },
        ];
        let mut render = std::ptr::null_mut();
        // SAFETY: an initialized handle and a terminated parameter list.
        let code = unsafe { (lib.render_create)(&mut render, mpv, params.as_mut_ptr()) };
        if code < 0 {
            return Err(fail("Couldn't start the video renderer", code));
        }
        for (id, name, format) in [(1, "pause", FORMAT_FLAG), (2, "time-pos", FORMAT_DOUBLE), (3, "duration", FORMAT_DOUBLE), (4, "paused-for-cache", FORMAT_FLAG), (5, "volume", FORMAT_DOUBLE), (6, "mute", FORMAT_FLAG), (7, "eof-reached", FORMAT_FLAG), (8, "seeking", FORMAT_FLAG)] {
            let n = CString::new(name).expect("property");
            // SAFETY: an initialized handle.
            unsafe { (lib.observe_property)(mpv, id, n.as_ptr(), format) };
        }
        let player = Self {
            mpv: Ptr(mpv),
            stopping: Arc::default(),
            alive: Arc::default(),
            wake: Arc::default(),
            width: Arc::new(AtomicU32::new(640)),
            height: Arc::new(AtomicU32::new(360)),
        };
        let deliver: Arc<dyn Fn(Update) + Send + Sync> = Arc::new(deliver);
        let wake = player.wake.clone();
        // SAFETY: the Wake outlives the context: the render thread frees the
        // context before its last reference to the Wake goes.
        unsafe { (lib.render_set_update_callback)(render, Some(render_wanted), Arc::as_ptr(&wake).cast_mut().cast()) };
        let renderer = {
            let (stopping, width, height, deliver, render) = (player.stopping.clone(), player.width.clone(), player.height.clone(), deliver.clone(), Ptr(render));
            std::thread::Builder::new().name("clarp-video-render".into()).spawn(move || render_loop(lib, render, &wake, &stopping, &width, &height, &*deliver))
        };
        let renderer = match renderer {
            Ok(thread) => thread,
            Err(error) => {
                // SAFETY: nothing else uses the context or handle yet.
                unsafe { (lib.render_free)(render) };
                return Err(fail(&format!("Couldn't start the video renderer ({error})"), ERROR_GENERIC));
            }
        };
        {
            let (stopping, alive, wake, mpv) = (player.stopping.clone(), player.alive.clone(), player.wake.clone(), player.mpv);
            let spawned = std::thread::Builder::new().name("clarp-video-events".into()).spawn(move || {
                event_loop(lib, mpv, &stopping, &*deliver);
                // From here the window's calls leave mpv alone (they check
                // `stopping` holding `alive`), also when mpv ended by itself.
                {
                    let _alive = alive.lock();
                    stopping.store(true, Ordering::SeqCst);
                }
                // Tear down: the renderer first (it frees the context), then mpv.
                wake.ring();
                let _ = renderer.join();
                // SAFETY: no thread uses the handle any more.
                unsafe { (lib.terminate_destroy)(mpv.0) };
                sources().lock().expect("sources").remove(&source);
            });
            if spawned.is_err() {
                player.stopping.store(true, Ordering::SeqCst);
                player.wake.ring();
                return Err("Couldn't start the video player".into());
            }
        }
        let uri = CString::new(format!("{PROTOCOL}://{source}")).expect("uri");
        let load = CString::new("loadfile").expect("command");
        player.command(&[load.as_c_str(), uri.as_c_str()]);
        Ok(player)
    }

    fn command(&self, args: &[&CStr]) {
        let Ok(lib) = lib() else { return };
        let _alive = self.alive.lock();
        if self.stopping.load(Ordering::SeqCst) {
            return;
        }
        let mut argv: Vec<*const c_char> = args.iter().map(|a| a.as_ptr()).collect();
        argv.push(std::ptr::null());
        // SAFETY: a live handle (the player isn't dropped) and a NULL-ended
        // argv, which mpv copies before returning.
        unsafe { (lib.command_async)(self.mpv.0, 0, argv.as_mut_ptr()) };
    }

    fn set_flag(&self, name: &str, value: bool) {
        let Ok(lib) = lib() else { return };
        let _alive = self.alive.lock();
        if self.stopping.load(Ordering::SeqCst) {
            return;
        }
        let n = CString::new(name).expect("property");
        let mut flag: c_int = value.into();
        // SAFETY: a live handle; FLAG takes an int, copied before returning.
        unsafe { (lib.set_property_async)(self.mpv.0, 0, n.as_ptr(), FORMAT_FLAG, (&raw mut flag).cast()) };
    }

    fn set_double(&self, name: &str, value: f64) {
        let Ok(lib) = lib() else { return };
        let _alive = self.alive.lock();
        if self.stopping.load(Ordering::SeqCst) {
            return;
        }
        let n = CString::new(name).expect("property");
        let mut v = value;
        // SAFETY: a live handle; DOUBLE takes a double, copied before returning.
        unsafe { (lib.set_property_async)(self.mpv.0, 0, n.as_ptr(), FORMAT_DOUBLE, (&raw mut v).cast()) };
    }

    pub fn set_paused(&self, paused: bool) {
        self.set_flag("pause", paused);
    }

    /// Moves to `seconds` from the start, exactly.
    pub fn seek_to(&self, seconds: f64) {
        let to = CString::new(format!("{:.3}", seconds.max(0.0))).expect("seconds");
        let (seek, absolute) = (CString::new("seek").expect("seek"), CString::new("absolute+exact").expect("flags"));
        self.command(&[seek.as_c_str(), to.as_c_str(), absolute.as_c_str()]);
    }

    pub fn set_volume(&self, volume: f64) {
        self.set_double("volume", volume.clamp(0.0, 100.0));
    }

    pub fn set_muted(&self, muted: bool) {
        self.set_flag("mute", muted);
    }

    /// The surface's size in physical pixels: pictures are drawn to fit it.
    pub fn resize(&self, width: u32, height: u32) {
        let (w, h) = fit_pixels(width, height);
        let changed = self.width.swap(w, Ordering::SeqCst) != w;
        let changed = self.height.swap(h, Ordering::SeqCst) != h || changed;
        if changed {
            self.wake.ring();
        }
    }
}

impl Drop for Player {
    fn drop(&mut self) {
        // The sound stops now, whatever teardown takes; nothing here waits
        // for mpv (the event thread tears it down).
        let stop = CString::new("stop").expect("stop");
        self.command(&[stop.as_c_str()]);
        let alive = self.alive.lock();
        let live = !self.stopping.swap(true, Ordering::SeqCst);
        self.wake.ring();
        if live && let Ok(lib) = lib() {
            // SAFETY: `alive` keeps the handle from being destroyed until
            // this returns; wakes the event thread to tear down.
            unsafe { (lib.wakeup)(self.mpv.0) };
        }
        drop(alive);
    }
}

/// The size pictures are drawn at: at least 16 px, at most `MAX_PIXELS`.
fn fit_pixels(width: u32, height: u32) -> (u32, u32) {
    let (mut w, mut h) = (width.max(16), height.max(16));
    let area = u64::from(w) * u64::from(h);
    if area > u64::from(MAX_PIXELS) {
        let scale = (f64::from(MAX_PIXELS) / area as f64).sqrt();
        w = ((f64::from(w) * scale) as u32).max(16);
        h = ((f64::from(h) * scale) as u32).max(16);
    }
    (w, h)
}

fn render_loop(lib: &'static Lib, render: Ptr, wake: &Wake, stopping: &AtomicBool, width: &AtomicU32, height: &AtomicU32, deliver: &(dyn Fn(Update) + Send + Sync)) {
    let mut drawn = (0, 0);
    let mut have_frame = false;
    loop {
        {
            let mut pending = wake.pending.lock().expect("wake");
            while !*pending && !stopping.load(Ordering::SeqCst) {
                pending = wake.ready.wait(pending).expect("wake");
            }
            *pending = false;
        }
        if stopping.load(Ordering::SeqCst) {
            break;
        }
        // SAFETY: the context is live until this thread frees it below.
        let flags = unsafe { (lib.render_update)(render.0) };
        let size = (width.load(Ordering::SeqCst), height.load(Ordering::SeqCst));
        let new_frame = flags & RENDER_UPDATE_FRAME != 0;
        if !new_frame && !(have_frame && size != drawn) {
            continue;
        }
        let mut buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::new(size.0, size.1);
        let mut dims = [size.0 as c_int, size.1 as c_int];
        let mut format = *b"rgb0\0";
        let mut stride: usize = size.0 as usize * 4;
        let mut params = [
            RenderParam { kind: RENDER_PARAM_SW_SIZE, data: dims.as_mut_ptr().cast() },
            RenderParam { kind: RENDER_PARAM_SW_FORMAT, data: format.as_mut_ptr().cast() },
            RenderParam { kind: RENDER_PARAM_SW_STRIDE, data: (&raw mut stride).cast() },
            RenderParam { kind: RENDER_PARAM_SW_POINTER, data: buffer.make_mut_bytes().as_mut_ptr().cast() },
            RenderParam { kind: RENDER_PARAM_INVALID, data: std::ptr::null_mut() },
        ];
        // SAFETY: the buffer holds stride * height bytes and outlives the call.
        let code = unsafe { (lib.render)(render.0, params.as_mut_ptr()) };
        if code < 0 {
            continue;
        }
        // "rgb0" leaves the fourth byte unset: make every pixel opaque.
        for pixel in buffer.make_mut_slice() {
            pixel.a = 255;
        }
        drawn = size;
        have_frame = true;
        deliver(Update::Frame(buffer));
    }
    // SAFETY: this thread is the context's only user; mpv is still alive
    // (the event thread destroys it after joining this one).
    unsafe { (lib.render_free)(render.0) };
}

fn event_loop(lib: &'static Lib, mpv: Ptr, stopping: &AtomicBool, deliver: &(dyn Fn(Update) + Send + Sync)) {
    let mut state = State { paused: true, volume: 100.0, ..State::default() };
    let mut sent = state.clone();
    let mut last_sent = Instant::now() - Duration::from_secs(1);
    while !stopping.load(Ordering::SeqCst) {
        // SAFETY: this thread is the handle's only event waiter.
        let event = unsafe { &*(lib.wait_event)(mpv.0, 0.25) };
        match event.event_id {
            EVENT_SHUTDOWN => break,
            EVENT_FILE_LOADED => {
                state.loaded = true;
                state.failed = None;
            }
            EVENT_END_FILE if !event.data.is_null() => {
                // SAFETY: END_FILE carries an mpv_event_end_file.
                let end = unsafe { &*event.data.cast::<RawEndFile>() };
                if end.reason == END_FILE_ERROR {
                    eprintln!("clarp-slint video: playback failed: {}", error_text(lib, end.error));
                    state.failed = Some("Couldn't play this video".into());
                }
            }
            EVENT_PROPERTY_CHANGE if !event.data.is_null() => {
                // SAFETY: PROPERTY_CHANGE carries an mpv_event_property.
                let property = unsafe { &*event.data.cast::<RawProperty>() };
                // SAFETY: data matches format when it isn't NONE (null).
                let flag = || (property.format == FORMAT_FLAG && !property.data.is_null()).then(|| unsafe { *property.data.cast::<c_int>() } != 0);
                // SAFETY: as above.
                let double = || (property.format == FORMAT_DOUBLE && !property.data.is_null()).then(|| unsafe { *property.data.cast::<f64>() });
                match event.reply_userdata {
                    1 => state.paused = flag().unwrap_or(state.paused),
                    2 => state.position = double().unwrap_or(state.position),
                    3 => state.duration = double().unwrap_or(state.duration),
                    4 => state.buffering = flag().unwrap_or(false),
                    5 => state.volume = double().unwrap_or(state.volume),
                    6 => state.muted = flag().unwrap_or(state.muted),
                    7 => state.ended = flag().unwrap_or(false),
                    _ => {}
                }
            }
            _ => {}
        }
        // Position alone goes out ten times a second; anything else at once.
        let only_moved = State { position: sent.position, ..state.clone() } == sent;
        if state != sent && (!only_moved || last_sent.elapsed() >= Duration::from_millis(100)) {
            deliver(Update::State(state.clone()));
            sent = state.clone();
            last_sent = Instant::now();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pictures_fit_the_limits() {
        assert_eq!(fit_pixels(0, 0), (16, 16));
        assert_eq!(fit_pixels(1280, 720), (1280, 720));
        let (w, h) = fit_pixels(7680, 4320);
        assert!(w * h <= MAX_PIXELS && w > 3000, "{w}x{h}");
    }
}
