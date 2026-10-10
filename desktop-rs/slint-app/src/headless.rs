//! Running the window without a display: Slint's software renderer into a
//! memory buffer, with our own event loop (so engine wakes and timers work),
//! for checks and screenshots. Nothing is shown on the user's desktop.

use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use slint::platform::software_renderer::{MinimalSoftwareWindow, PremultipliedRgbaColor, RepaintBufferType};
use slint::platform::{EventLoopProxy, Platform, WindowAdapter, WindowEvent};
use slint::{EventLoopError, PhysicalSize, PlatformError, SharedString};

type Task = Box<dyn FnOnce() + Send>;

#[derive(Default)]
struct Queue {
    tasks: Mutex<VecDeque<Task>>,
    ready: Condvar,
    quit: AtomicBool,
}

struct Headless {
    window: Rc<MinimalSoftwareWindow>,
    queue: Arc<Queue>,
    size: (u32, u32),
}

struct Proxy(Arc<Queue>);

impl EventLoopProxy for Proxy {
    fn quit_event_loop(&self) -> Result<(), EventLoopError> {
        self.0.quit.store(true, Ordering::SeqCst);
        self.0.ready.notify_all();
        Ok(())
    }
    fn invoke_from_event_loop(&self, task: Box<dyn FnOnce() + Send>) -> Result<(), EventLoopError> {
        self.0.tasks.lock().map_err(|_| EventLoopError::EventLoopTerminated)?.push_back(task);
        self.0.ready.notify_all();
        Ok(())
    }
}

/// How headless frames are drawn (the frame-budget check): `partial`
/// repaints only what changed into the kept buffer, as the real window does;
/// `hz` above 0 draws at most that many frames a second, as a display's
/// frame callbacks allow. The default draws every frame whole, at once.
#[derive(Debug, Clone, Copy, Default)]
pub struct Pacing {
    pub partial: bool,
    pub hz: u32,
}

/// Sets how frames are drawn from now on.
pub fn pace(pacing: Pacing) {
    PACING.with(|p| p.set(pacing));
}

/// The next frame repaints the whole window (and is drawn even if nothing
/// changed): one full frame's cost.
pub fn full_frame() {
    FULL_NEXT.with(|f| f.set(true));
    if let Some(window) = window() {
        window.request_redraw();
    }
}

thread_local! {
    static PACING: Cell<Pacing> = const { Cell::new(Pacing { partial: false, hz: 0 }) };
    static LAST_DRAWN: Cell<Option<std::time::Instant>> = const { Cell::new(None) };
    static FULL_NEXT: Cell<bool> = const { Cell::new(false) };
    static WINDOW: RefCell<Option<Rc<MinimalSoftwareWindow>>> = const { RefCell::new(None) };
    /// The last frame drawn (width, height, pixels) and how many so far.
    static FRAME: RefCell<(usize, usize, Vec<PremultipliedRgbaColor>, u64)> = const { RefCell::new((0, 0, Vec::new(), 0)) };
}

impl Platform for Headless {
    fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, PlatformError> {
        Ok(self.window.clone())
    }

    fn new_event_loop_proxy(&self) -> Option<Box<dyn EventLoopProxy>> {
        Some(Box::new(Proxy(self.queue.clone())))
    }

    fn run_event_loop(&self) -> Result<(), PlatformError> {
        self.window.set_size(PhysicalSize::new(self.size.0, self.size.1));
        self.window.dispatch_event(WindowEvent::WindowActiveChanged(true));
        loop {
            slint::platform::update_timers_and_animations();
            let tasks: Vec<Task> = match self.queue.tasks.lock() {
                Ok(mut tasks) => tasks.drain(..).collect(),
                Err(_) => return Err(PlatformError::Other("the event queue is poisoned".into())),
            };
            for task in tasks {
                task();
            }
            if self.queue.quit.load(Ordering::SeqCst) {
                return Ok(());
            }
            // No resize after a scale change here: the app's own
            // (`desktop::rescale`) must keep the window in its pixels, or this
            // renderer panics as the real window's does (the avatars check
            // scales to 1.5).
            let size = self.window.size();
            let (width, height) = (size.width as usize, size.height as usize);
            // The frame-budget check paces frames as a display does; the
            // others draw as soon as something asks.
            let pacing = PACING.with(Cell::get);
            let interval = (pacing.hz > 0).then(|| Duration::from_secs_f64(1.0 / f64::from(pacing.hz)));
            let since = LAST_DRAWN.with(Cell::get).map(|at| at.elapsed());
            let early = interval.zip(since).and_then(|(interval, since)| interval.checked_sub(since)).filter(|left| !left.is_zero());
            if early.is_none() {
                let drew = self.window.draw_if_needed(|renderer| {
                    FRAME.with(|frame| {
                        let mut frame = frame.borrow_mut();
                        // A margin: at a fractional scale the renderer may round
                        // the window a pixel past its size. Saving crops it.
                        let stride = width + MARGIN;
                        let forced = FULL_NEXT.with(|f| f.replace(false));
                        let mut full = !pacing.partial || forced;
                        if frame.0 != width || frame.1 != height {
                            *frame = (width, height, vec![PremultipliedRgbaColor::default(); stride * (height + MARGIN)], frame.3);
                            full = true;
                        }
                        // Partial: only what changed is repainted into the kept
                        // buffer, as the real window's softbuffer does with a
                        // buffer age of one.
                        renderer.set_repaint_buffer_type(if full { RepaintBufferType::NewBuffer } else { RepaintBufferType::ReusedBuffer });
                        let started = std::time::Instant::now();
                        let region = renderer.render(&mut frame.2, stride);
                        let rects: Vec<[u32; 4]> = region.iter().map(|(at, size)| [at.x.max(0) as u32, at.y.max(0) as u32, size.width, size.height]).collect();
                        let painted: u64 = rects.iter().map(|r| u64::from(r[2]) * u64::from(r[3])).sum();
                        let dirty = (painted as f64 / (width.max(1) * height.max(1)) as f64).min(1.0) as f32;
                        crate::perf::drawn(started, dirty, rects);
                        frame.3 += 1;
                    });
                });
                if drew {
                    LAST_DRAWN.with(|l| l.set(Some(std::time::Instant::now())));
                }
            }
            let wait = slint::platform::duration_until_next_timer_update()
                .unwrap_or(Duration::from_millis(50))
                .min(Duration::from_millis(16))
                .min(early.unwrap_or(Duration::MAX));
            if let Ok(tasks) = self.queue.tasks.lock()
                && tasks.is_empty()
                && !self.queue.quit.load(Ordering::SeqCst)
            {
                let _ = self.queue.ready.wait_timeout(tasks, wait);
            }
        }
    }
}

/// The headless window's logical size and scale: 1280×800 at 1, unless
/// `CLARP_HEADLESS_SIZE` says otherwise ("1600x1000@1.5", as the
/// frame-budget check draws a laptop's panel).
pub fn size_from_env() -> (u32, u32, f32) {
    let Ok(value) = std::env::var("CLARP_HEADLESS_SIZE") else { return (1280, 800, 1.0) };
    let parsed = (|| {
        let (size, scale) = value.split_once('@').unwrap_or((value.as_str(), "1"));
        let (width, height) = size.split_once('x')?;
        Some((width.parse().ok()?, height.parse().ok()?, scale.parse().ok()?))
    })();
    parsed.unwrap_or_else(|| {
        eprintln!("clarp-slint: CLARP_HEADLESS_SIZE={value} is not WIDTHxHEIGHT[@SCALE]; drawing 1280x800");
        (1280, 800, 1.0)
    })
}

/// Installs the headless platform for a `width`×`height` logical window.
pub fn install(width: u32, height: u32, scale: f32) -> Result<(), String> {
    let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
    window.dispatch_event(WindowEvent::ScaleFactorChanged { scale_factor: scale });
    WINDOW.with(|w| *w.borrow_mut() = Some(window.clone()));
    let size = ((width as f32 * scale) as u32, (height as f32 * scale) as u32);
    slint::platform::set_platform(Box::new(Headless { window, queue: Arc::new(Queue::default()), size }))
        .map_err(|e| format!("cannot install the headless platform: {e}"))
}

fn window() -> Option<Rc<MinimalSoftwareWindow>> {
    WINDOW.with(|w| w.borrow().clone())
}

/// Presses and releases one key (a character, or a `slint::platform::Key`).
pub fn press(key: impl Into<SharedString>) {
    let Some(window) = window() else { return };
    let text: SharedString = key.into();
    window.dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
    window.dispatch_event(WindowEvent::KeyReleased { text });
}

/// `key` with `modifiers` held (e.g. Control, Shift), as a keyboard sends it.
pub fn press_with(modifiers: &[slint::platform::Key], key: impl Into<SharedString>) {
    let Some(window) = window() else { return };
    for modifier in modifiers {
        window.dispatch_event(WindowEvent::KeyPressed { text: (*modifier).into() });
    }
    press(key);
    for modifier in modifiers.iter().rev() {
        window.dispatch_event(WindowEvent::KeyReleased { text: (*modifier).into() });
    }
}

/// Puts one key down and leaves it there (no release), as when the window
/// loses the keyboard while the key is still held.
pub fn hold(key: impl Into<SharedString>) {
    let Some(window) = window() else { return };
    window.dispatch_event(WindowEvent::KeyPressed { text: key.into() });
}

/// Lets go of a key put down with `hold`.
pub fn release(key: impl Into<SharedString>) {
    let Some(window) = window() else { return };
    window.dispatch_event(WindowEvent::KeyReleased { text: key.into() });
}

/// The window system gives the keyboard to another window (false) or back
/// to this one (true), as winit's `Focused` does.
pub fn set_active(active: bool) {
    let Some(window) = window() else { return };
    window.dispatch_event(WindowEvent::WindowActiveChanged(active));
}

/// The window system says which modifiers are held, as winit's
/// `ModifiersChanged` does after each modifier key.
pub fn modifiers_changed(modifiers: crate::platform::keyboard::Modifiers) {
    crate::platform::keyboard::reported(modifiers);
}

/// The pointer leaves the window.
pub fn pointer_exit() {
    let Some(window) = window() else { return };
    window.dispatch_event(WindowEvent::PointerExited);
}

/// The pointer moves to a logical position (entering the window).
pub fn pointer_move(x: f32, y: f32) {
    let Some(window) = window() else { return };
    window.dispatch_event(WindowEvent::PointerMoved { position: slint::LogicalPosition::new(x, y) });
}

/// A left click at a logical position, as a mouse sends it.
pub fn click(x: f32, y: f32) {
    use slint::platform::PointerEventButton;
    let Some(window) = window() else { return };
    let position = slint::LogicalPosition::new(x, y);
    window.dispatch_event(WindowEvent::PointerMoved { position });
    window.dispatch_event(WindowEvent::PointerPressed { position, button: PointerEventButton::Left });
    window.dispatch_event(WindowEvent::PointerReleased { position, button: PointerEventButton::Left });
}

/// A mouse wheel turn over a logical position, as winit delivers it (a
/// `Moved` phase, which Slint animates): a positive `delta_y` scrolls up
/// (towards older content). A real wheel line is 60 px.
pub fn wheel(x: f32, y: f32, delta_y: f32) {
    scroll(x, y, delta_y, Phase::Moved);
}

/// The phase of a touchpad gesture (winit's `TouchPhase`).
#[derive(Debug, Clone, Copy)]
pub enum Phase {
    Started,
    Moved,
    Ended,
}

/// One touchpad (or wheel) scroll event with its gesture phase.
pub fn scroll(x: f32, y: f32, delta_y: f32, phase: Phase) {
    use i_slint_core::input::{BackendMouseEvent, TouchPhase};
    let Some(window) = window() else { return };
    let position = slint::LogicalPosition::new(x, y);
    window.dispatch_event(WindowEvent::PointerMoved { position });
    let phase = match phase {
        Phase::Started => TouchPhase::Started,
        Phase::Moved => TouchPhase::Moved,
        Phase::Ended => TouchPhase::Ended,
    };
    window.dispatch_event(WindowEvent::internal(BackendMouseEvent::Wheel {
        position: i_slint_core::lengths::LogicalPoint::new(x as _, y as _),
        delta_x: 0.0,
        delta_y: delta_y as _,
        phase,
    }));
}

pub fn type_text(text: &str) {
    for character in text.chars() {
        press(SharedString::from(character.to_string()));
    }
}

const MARGIN: usize = 8;

/// How many frames have been drawn so far.
pub fn frames_drawn() -> u64 {
    FRAME.with(|frame| frame.borrow().3)
}

/// Saves the last drawn frame as PNG.
pub fn save_frame(path: &str) -> Result<(), String> {
    FRAME.with(|frame| {
        let frame = frame.borrow();
        let (width, height, pixels) = (frame.0, frame.1, &frame.2);
        if pixels.is_empty() {
            return Err("nothing has been drawn yet".into());
        }
        let stride = width + MARGIN;
        let mut image = image::RgbaImage::new(width as u32, height as u32);
        for (x, y, pixel) in image.enumerate_pixels_mut() {
            let color = pixels[y as usize * stride + x as usize];
            *pixel = image::Rgba([color.red, color.green, color.blue, 255]);
        }
        image.save(path).map_err(|e| format!("cannot save {path}: {e}"))
    })
}
