//! KeyInjector: real key presses into one of this process's windows, for
//! probes (C++ `KeyboardSmokeCheck` and friends). Keys go through Qt's
//! window-system path (`QTest::keyClick`), exactly as typing would; it only
//! ever acts on the offscreen platform, so it can never type into the
//! user's desktop.

use cxx_qt_lib::{KeyboardModifiers, QString};

#[cxx_qt::bridge]
pub mod qobject {
    /// The `Qt::Key` values probes press (the enum is Qt's; cxx checks each).
    #[namespace = "Qt"]
    #[repr(i32)]
    enum Key {
        Key_Escape = 0x0100_0000,
        Key_Tab = 0x0100_0001,
        Key_Backtab = 0x0100_0002,
        Key_Backspace = 0x0100_0003,
        Key_Return = 0x0100_0004,
        Key_Enter = 0x0100_0005,
        Key_Delete = 0x0100_0007,
        Key_Home = 0x0100_0010,
        Key_End = 0x0100_0011,
        Key_Left = 0x0100_0012,
        Key_Up = 0x0100_0013,
        Key_Right = 0x0100_0014,
        Key_Down = 0x0100_0015,
        Key_PageUp = 0x0100_0016,
        Key_PageDown = 0x0100_0017,
        Key_Space = 0x20,
        Key_Slash = 0x2f,
        Key_A = 0x41,
    }

    unsafe extern "C++" {
        include!(<QtTest/QTest>);
        include!(<QtGui/QWindow>);
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qt.h");
        #[namespace = "Qt"]
        type KeyboardModifiers = cxx_qt_lib::KeyboardModifiers;
        #[namespace = "Qt"]
        type Key;
        type QWindow;
        include!(<QtGui/QGuiApplication>);
        type QGuiApplication;
        #[Self = "QGuiApplication"]
        #[cxx_name = "platformName"]
        fn gui_platform_name() -> QString;
        #[namespace = "QTest"]
        #[cxx_name = "keyClick"]
        unsafe fn key_click(window: *mut QWindow, key: Key, modifier: KeyboardModifiers, delay: i32);
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        type KeyInjector = super::KeyInjectorRust;
    }

    unsafe extern "RustQt" {
        /// Presses and releases `key` (a Qt.Key_* value; letters and digits
        /// by their code) with `modifiers` (Qt.ControlModifier | ...) in
        /// `window`. False, and nothing sent, off the offscreen platform.
        #[qinvokable]
        unsafe fn press(self: &KeyInjector, window: *mut QWindow, key: i32, modifiers: i32) -> bool;
        /// Types `text` one key at a time.
        #[qinvokable]
        #[cxx_name = "type"]
        unsafe fn type_text(self: &KeyInjector, window: *mut QWindow, text: &QString) -> bool;
    }
}

#[derive(Default)]
pub struct KeyInjectorRust;

fn offscreen() -> bool {
    let platform = qobject::QGuiApplication::gui_platform_name().to_string();
    if platform != "offscreen" {
        eprintln!("KeyInjector: refused on the {platform} platform; keys only go to offscreen windows");
        return false;
    }
    true
}

impl qobject::KeyInjector {
    unsafe fn press(&self, window: *mut qobject::QWindow, key: i32, modifiers: i32) -> bool {
        if window.is_null() || !offscreen() {
            return false;
        }
        let flags = KeyboardModifiers::from_int(modifiers as u32);
        unsafe { qobject::key_click(window, qobject::Key { repr: key }, flags, -1) };
        true
    }

    unsafe fn type_text(&self, window: *mut qobject::QWindow, text: &QString) -> bool {
        if window.is_null() || !offscreen() {
            return false;
        }
        for character in text.to_string().chars() {
            let upper = character.to_ascii_uppercase();
            let shift = if character.is_ascii_uppercase() { 0x0200_0000 } else { 0 };
            unsafe { qobject::key_click(window, qobject::Key { repr: upper as i32 }, KeyboardModifiers::from_int(shift), -1) };
        }
        true
    }
}
