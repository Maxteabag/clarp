//! The desktop services the controller hands work to: the default handler
//! for a URL, the clipboard, and which windowing platform runs. URL opening
//! and the platform are Qt's own declarations (cxx-qt-lib does not bind
//! them); the clipboard is `arboard`, because cxx cannot name Qt's nested
//! `QClipboard::Mode`. `CLARP_TEST_CLIPBOARD=<file>` writes copies to a file
//! instead, so tests never touch the user's clipboard.

#[cxx_qt::bridge]
pub mod ffi {
    unsafe extern "C++" {
        include!(<QtGui/QDesktopServices>);
        include!(<QtGui/QGuiApplication>);
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qurl.h");
        type QUrl = cxx_qt_lib::QUrl;

        type QDesktopServices;
        #[Self = "QDesktopServices"]
        #[cxx_name = "openUrl"]
        fn open_url(url: &QUrl) -> bool;

        type QGuiApplication;
        #[Self = "QGuiApplication"]
        #[cxx_name = "platformName"]
        fn platform_name() -> QString;
    }
}

use std::sync::{Mutex, OnceLock};

use cxx_qt_lib::QUrl;

/// The desktop's handler for `url`. `CLARP_TEST_OPEN_URL=<file>` appends
/// the URL to that file instead, so tests never open the user's browser.
pub fn open_url(url: &str) -> bool {
    if let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL") {
        use std::io::Write;
        let appended = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{url}"));
        if let Err(error) = &appended {
            eprintln!("desktop: could not record {url}: {error}");
        }
        return appended.is_ok();
    }
    ffi::QDesktopServices::open_url(&QUrl::from(url))
}

pub fn testing_urls() -> bool {
    std::env::var_os("CLARP_TEST_OPEN_URL").is_some()
}

/// The clipboard stays open for the whole run: on Linux the copying
/// process serves the text until something else is copied.
fn clipboard() -> &'static Mutex<Option<arboard::Clipboard>> {
    static CLIPBOARD: OnceLock<Mutex<Option<arboard::Clipboard>>> = OnceLock::new();
    CLIPBOARD.get_or_init(|| {
        Mutex::new(match arboard::Clipboard::new() {
            Ok(clipboard) => Some(clipboard),
            Err(error) => {
                eprintln!("desktop: no clipboard: {error}");
                None
            }
        })
    })
}

pub fn copy_to_clipboard(text: &str) -> Result<(), String> {
    if let Some(path) = std::env::var_os("CLARP_TEST_CLIPBOARD") {
        return std::fs::write(&path, text).map_err(|e| format!("{}: {e}", std::path::Path::new(&path).display()));
    }
    let mut guard = clipboard().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let clipboard = guard.as_mut().ok_or("No clipboard is available")?;
    clipboard.set_text(text).map_err(|e| e.to_string())
}

/// The clipboard's image as PNG, if it holds one. With `CLARP_TEST_CLIPBOARD`
/// the test file is the clipboard: PNG bytes are an image, anything else text.
pub fn clipboard_image_png() -> Option<Vec<u8>> {
    if let Some(path) = std::env::var_os("CLARP_TEST_CLIPBOARD") {
        return std::fs::read(&path).ok().filter(|bytes| clarp_core::media::is_png(bytes));
    }
    let mut guard = clipboard().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let image = guard.as_mut()?.get_image().ok()?;
    clarp_core::media::png_from_rgba(image.width as u32, image.height as u32, image.bytes.into_owned())
}

pub fn platform_name() -> String {
    ffi::QGuiApplication::platform_name().to_string()
}
