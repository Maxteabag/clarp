//! WindowCapture: saves a window as it is drawn (`QQuickWindow::grabWindow`),
//! for screenshot runs. QML's `grabToImage` cannot grab a window's own root.

use std::mem::MaybeUninit;
use std::pin::Pin;

use cxx::{ExternType, type_id};
use cxx_qt_lib::QString;

/// The same C++ `QImage` as `cxx_qt_lib::QImage`, as a type of this crate
/// so `QImage::save` can be bound on it; it is handed back to cxx-qt-lib's
/// type, whose `Drop` runs the C++ destructor.
#[repr(C)]
pub struct GrabbedImage {
    _space: [MaybeUninit<usize>; std::mem::size_of::<cxx_qt_lib::QImage>() / std::mem::size_of::<usize>()],
}

const _: () = assert!(std::mem::size_of::<GrabbedImage>() == std::mem::size_of::<cxx_qt_lib::QImage>());
const _: () = assert!(std::mem::align_of::<GrabbedImage>() == std::mem::align_of::<cxx_qt_lib::QImage>());

// SAFETY: identical layout to cxx_qt_lib::QImage, which cxx-qt-lib checks
// against the C++ class; trivially relocatable like it.
unsafe impl ExternType for GrabbedImage {
    type Id = type_id!("QImage");
    type Kind = cxx::kind::Trivial;
}

impl Drop for GrabbedImage {
    fn drop(&mut self) {
        // SAFETY: same type and layout; cxx-qt-lib's Drop destroys it.
        let image: cxx_qt_lib::QImage = unsafe { std::ptr::read((self as *const Self).cast()) };
        drop(image);
    }
}

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qimage.h");
        type QImage = super::GrabbedImage;
        include!(<QtQuick/QQuickWindow>);
        type QQuickWindow;
        #[cxx_name = "grabWindow"]
        fn grab_window(self: Pin<&mut QQuickWindow>) -> QImage;
        unsafe fn save(self: &QImage, file_name: &QString, format: *const c_char, quality: i32) -> bool;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        type WindowCapture = super::WindowCaptureRust;
    }

    unsafe extern "RustQt" {
        /// Grabs `window` and saves it as PNG; false when either fails.
        #[qinvokable]
        unsafe fn capture(self: &WindowCapture, window: *mut QQuickWindow, path: &QString) -> bool;
    }
}

#[derive(Default)]
pub struct WindowCaptureRust;

impl qobject::WindowCapture {
    unsafe fn capture(&self, window: *mut qobject::QQuickWindow, path: &QString) -> bool {
        let Some(window) = (unsafe { window.as_mut() }) else {
            eprintln!("WindowCapture: no window to grab");
            return false;
        };
        let image = unsafe { Pin::new_unchecked(window) }.grab_window();
        let saved = unsafe { image.save(path, c"PNG".as_ptr(), -1) };
        if !saved {
            eprintln!("WindowCapture: could not save {path}");
        }
        saved
    }
}
