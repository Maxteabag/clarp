//! AvatarMotionClock: QObject over `clarp_core::avatar_motion::MotionClock`,
//! owned by the AppController (`avatarMotion`). Runs the frame and process
//! timers only while the core says something visible needs them, and stops
//! animating while the app is in the background.

use std::pin::Pin;
use std::time::{Duration, Instant};

use clarp_core::avatar_motion::{FRAME_MS, MotionClock, PROCESS_MS, Timers};
use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::QString;

use super::quick::{self, ffi as q};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!(<QtGui/QGuiApplication>);
        type QGuiApplication;
        #[Self = "QGuiApplication"]
        #[cxx_name = "applicationState"]
        fn application_state() -> ApplicationState;
        type QCoreApplication;
        #[Self = "QCoreApplication"]
        #[cxx_name = "instance"]
        fn application() -> *mut QCoreApplication;
    }

    #[namespace = "Qt"]
    #[repr(i32)]
    enum ApplicationState {
        ApplicationSuspended = 0,
        ApplicationHidden = 1,
        ApplicationInactive = 2,
        ApplicationActive = 4,
    }

    #[namespace = "Qt"]
    unsafe extern "C++" {
        type ApplicationState;
    }

    extern "RustQt" {
        #[qobject]
        #[qproperty(bool, ticking, READ = ticking_value, NOTIFY = changed)]
        #[qproperty(u64, revision, READ = revision_value, NOTIFY = tick)]
        #[qproperty(u64, working_revision, cxx_name = "workingRevision", READ = working_revision_value, NOTIFY = changed)]
        #[qproperty(bool, reduced_motion, cxx_name = "reducedMotion", READ = reduced_motion_value, WRITE = set_reduced_motion, NOTIFY = changed)]
        #[qproperty(u64, process_revision, cxx_name = "processRevision", READ = process_revision_value, NOTIFY = process_tick)]
        type AvatarMotionClock = super::AvatarMotionRust;
    }

    unsafe extern "RustQt" {
        fn ticking_value(self: &AvatarMotionClock) -> bool;
        fn revision_value(self: &AvatarMotionClock) -> u64;
        fn working_revision_value(self: &AvatarMotionClock) -> u64;
        fn reduced_motion_value(self: &AvatarMotionClock) -> bool;
        #[cxx_name = "setReducedMotion"]
        fn set_reduced_motion(self: Pin<&mut AvatarMotionClock>, value: bool);
        fn process_revision_value(self: &AvatarMotionClock) -> u64;

        #[qsignal]
        fn changed(self: Pin<&mut AvatarMotionClock>);
        #[qsignal]
        fn tick(self: Pin<&mut AvatarMotionClock>);
        #[qsignal]
        #[cxx_name = "processTick"]
        fn process_tick(self: Pin<&mut AvatarMotionClock>);
        /// Forwarded from QGuiApplication::applicationStateChanged.
        #[qsignal]
        #[cxx_name = "applicationStateChanged"]
        fn application_state_changed(self: Pin<&mut AvatarMotionClock>);

        #[qinvokable]
        unsafe fn observe(self: Pin<&mut AvatarMotionClock>, owner: *mut QObject, visible: bool);
        #[qinvokable]
        #[cxx_name = "observeProcess"]
        unsafe fn observe_process(self: Pin<&mut AvatarMotionClock>, owner: *mut QObject, visible: bool);
        #[qinvokable]
        fn working(self: &AvatarMotionClock, session: &QString) -> bool;
        #[qinvokable]
        fn phase(self: &AvatarMotionClock, session: &QString) -> f64;
        #[qinvokable]
        #[cxx_name = "processHop"]
        fn process_hop(self: &AvatarMotionClock) -> i32;
    }

    impl cxx_qt::Threading for AvatarMotionClock {}
    impl cxx_qt::Initialize for AvatarMotionClock {}

    #[namespace = "rust::cxxqtlib1"]
    unsafe extern "C++" {
        include!("cxx-qt-lib/common.h");
        /// Owned by the AppController.
        #[rust_name = "new_avatar_motion_clock"]
        fn make_unique() -> UniquePtr<AvatarMotionClock>;
    }
}

pub struct AvatarMotionRust {
    clock: MotionClock,
    started: Instant,
    running: Timers,
    /// Bumped to stop a running timer loop.
    frame_generation: u64,
    process_generation: u64,
}

impl Default for AvatarMotionRust {
    fn default() -> Self {
        Self { clock: MotionClock::new(false), started: Instant::now(), running: Timers::default(), frame_generation: 0, process_generation: 0 }
    }
}

impl AvatarMotionRust {
    fn now_ms(&self) -> u64 {
        self.started.elapsed().as_millis() as u64
    }
}

impl cxx_qt::Initialize for qobject::AvatarMotionClock {
    fn initialize(mut self: Pin<&mut Self>) {
        let foreground = qobject::AvatarMotionClock::foreground();
        self.as_mut().rust_mut().clock.set_foreground(foreground);
        let application = qobject::QCoreApplication::application().cast::<q::QObject>().cast_const();
        if !application.is_null() {
            let receiver = (&*self as *const Self).cast::<q::QObject>();
            let guard = unsafe {
                quick::forward_signal(application, c"applicationStateChanged(Qt::ApplicationState)", receiver, c"applicationStateChanged()")
            };
            if let Some(guard) = guard {
                guard.release();
            }
        }
        self.as_mut()
            .on_application_state_changed(|mut this| {
                let foreground = qobject::AvatarMotionClock::foreground();
                this.as_mut().rust_mut().clock.set_foreground(foreground);
                this.as_mut().schedule();
                this.as_mut().changed();
                this.tick();
            })
            .release();
    }
}

impl qobject::AvatarMotionClock {
    fn foreground() -> bool {
        qobject::QGuiApplication::application_state() == qobject::ApplicationState::ApplicationActive
    }

    fn ticking_value(&self) -> bool {
        self.running.frame
    }
    fn revision_value(&self) -> u64 {
        self.clock.revision
    }
    fn working_revision_value(&self) -> u64 {
        self.clock.working_revision
    }
    fn reduced_motion_value(&self) -> bool {
        self.clock.reduced()
    }
    pub fn reduced_motion_value_pub(&self) -> bool {
        self.clock.reduced()
    }
    fn process_revision_value(&self) -> u64 {
        self.clock.process_revision
    }

    /// The controller persists reduced motion; this only applies it.
    pub fn set_reduced_motion(mut self: Pin<&mut Self>, value: bool) {
        if self.as_mut().rust_mut().clock.set_reduced(value) {
            self.as_mut().schedule();
            self.as_mut().changed();
            self.tick();
        }
    }

    /// The sessions that are working now (thinking, tool, compacting, running).
    pub fn reconcile(mut self: Pin<&mut Self>, active: &std::collections::HashSet<String>) {
        let now = self.now_ms();
        self.as_mut().rust_mut().clock.reconcile(active, now);
        self.as_mut().schedule();
        self.as_mut().changed();
        self.tick();
    }

    unsafe fn observe(mut self: Pin<&mut Self>, owner: *mut qobject::QObject, visible: bool) {
        if owner.is_null() {
            return;
        }
        self.as_mut().rust_mut().clock.observe(owner as usize, visible);
        self.schedule();
    }

    unsafe fn observe_process(mut self: Pin<&mut Self>, owner: *mut qobject::QObject, visible: bool) {
        if owner.is_null() {
            return;
        }
        self.as_mut().rust_mut().clock.observe_process(owner as usize, visible);
        self.schedule();
    }

    fn working(&self, session: &QString) -> bool {
        self.clock.working(&session.to_string())
    }

    fn phase(&self, session: &QString) -> f64 {
        self.clock.phase(&session.to_string(), self.now_ms())
    }

    fn process_hop(&self) -> i32 {
        self.clock.process_hop()
    }

    fn schedule(mut self: Pin<&mut Self>) {
        let wanted = self.clock.timers();
        let running = self.running;
        if wanted.frame && !running.frame {
            let generation = self.frame_generation;
            self.as_mut().frame_after(generation);
        } else if !wanted.frame && running.frame {
            self.as_mut().rust_mut().frame_generation += 1;
        }
        if wanted.process && !running.process {
            let generation = self.process_generation;
            self.as_mut().process_after(generation);
        } else if !wanted.process && running.process {
            self.as_mut().rust_mut().process_generation += 1;
        }
        if wanted.frame != running.frame {
            self.as_mut().rust_mut().running = wanted;
            self.changed();
        } else {
            self.as_mut().rust_mut().running = wanted;
        }
    }

    fn frame_after(self: Pin<&mut Self>, generation: u64) {
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_millis(FRAME_MS), move || {
            let queued = qt.queue(move |mut this| {
                if this.frame_generation != generation {
                    return;
                }
                this.as_mut().rust_mut().clock.revision += 1;
                this.as_mut().tick();
                this.frame_after(generation);
            });
            if queued.is_err() {
                eprintln!("AvatarMotionClock: frame timer stopped; the clock is gone");
            }
        });
    }

    fn process_after(self: Pin<&mut Self>, generation: u64) {
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_millis(PROCESS_MS), move || {
            let queued = qt.queue(move |mut this| {
                if this.process_generation != generation {
                    return;
                }
                this.as_mut().rust_mut().clock.process_revision += 1;
                this.as_mut().process_tick();
                this.process_after(generation);
            });
            if queued.is_err() {
                eprintln!("AvatarMotionClock: process timer stopped; the clock is gone");
            }
        });
    }
}
