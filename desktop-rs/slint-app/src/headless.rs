//! Running the window without a display: Slint's software renderer into a
//! memory buffer, with our own event loop (so engine wakes and timers work),
//! for checks and screenshots. Nothing is shown on the user's desktop.

use std::cell::RefCell;
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

thread_local! {
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
        let (width, height) = (self.size.0 as usize, self.size.1 as usize);
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
            self.window.draw_if_needed(|renderer| {
                FRAME.with(|frame| {
                    let mut frame = frame.borrow_mut();
                    if frame.2.len() != width * height {
                        *frame = (width, height, vec![PremultipliedRgbaColor::default(); width * height], frame.3);
                    }
                    renderer.render(&mut frame.2, width);
                    frame.3 += 1;
                });
            });
            let wait = slint::platform::duration_until_next_timer_update()
                .unwrap_or(Duration::from_millis(50))
                .min(Duration::from_millis(16));
            if let Ok(tasks) = self.queue.tasks.lock()
                && tasks.is_empty()
                && !self.queue.quit.load(Ordering::SeqCst)
            {
                let _ = self.queue.ready.wait_timeout(tasks, wait);
            }
        }
    }
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

pub fn type_text(text: &str) {
    for character in text.chars() {
        press(SharedString::from(character.to_string()));
    }
}

/// Saves the last drawn frame as PNG.
pub fn save_frame(path: &str) -> Result<(), String> {
    FRAME.with(|frame| {
        let frame = frame.borrow();
        let (width, height, pixels) = (frame.0, frame.1, &frame.2);
        if pixels.is_empty() {
            return Err("nothing has been drawn yet".into());
        }
        let mut image = image::RgbaImage::new(width as u32, height as u32);
        for (pixel, color) in image.pixels_mut().zip(pixels.iter()) {
            *pixel = image::Rgba([color.red, color.green, color.blue, 255]);
        }
        image.save(path).map_err(|e| format!("cannot save {path}: {e}"))
    })
}
