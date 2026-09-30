//! The clipboard (the Qt app's `desktop.rs`), through `arboard`.
//! `CLARP_TEST_CLIPBOARD=<file>` is the clipboard for checks: copies are
//! written there, and a PNG there is the image to paste, so checks never
//! touch the user's clipboard.

use std::sync::{Mutex, OnceLock};

/// The clipboard stays open for the whole run: on Linux the copying
/// process serves the text until something else is copied.
fn clipboard() -> &'static Mutex<Option<arboard::Clipboard>> {
    static CLIPBOARD: OnceLock<Mutex<Option<arboard::Clipboard>>> = OnceLock::new();
    CLIPBOARD.get_or_init(|| {
        Mutex::new(match arboard::Clipboard::new() {
            Ok(clipboard) => Some(clipboard),
            Err(error) => {
                eprintln!("clarp-slint: no clipboard: {error}");
                None
            }
        })
    })
}

#[allow(dead_code)] // for the transcript's copy action
#[allow(dead_code)] // for the transcript's copy action
pub fn copy(text: &str) -> Result<(), String> {
    if let Some(path) = std::env::var_os("CLARP_TEST_CLIPBOARD") {
        return std::fs::write(&path, text).map_err(|e| format!("{}: {e}", std::path::Path::new(&path).display()));
    }
    let mut guard = clipboard().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let clipboard = guard.as_mut().ok_or("No clipboard is available")?;
    clipboard.set_text(text).map_err(|e| e.to_string())
}

/// The clipboard's image as PNG, if it holds one.
pub fn image_png() -> Option<Vec<u8>> {
    if let Some(path) = std::env::var_os("CLARP_TEST_CLIPBOARD") {
        return std::fs::read(&path).ok().filter(|bytes| clarp_core::media::is_png(bytes));
    }
    let mut guard = clipboard().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let image = guard.as_mut()?.get_image().ok()?;
    clarp_core::media::png_from_rgba(image.width as u32, image.height as u32, image.bytes.into_owned())
}

/// A copied image becomes an attachment of `session`, without sending or
/// touching the draft (the Qt `pasteClipboardImage`). False when the
/// clipboard holds no image: the text paste proceeds.
pub fn paste_image(engine: &mut clarp_engine::Engine, session: &str) -> bool {
    const MAX_IMAGE_BYTES: usize = 256 * 1024 * 1024;
    let Some(png) = image_png() else { return false };
    if session.is_empty() {
        engine.report_error("Select a conversation before pasting an image");
        return true;
    }
    if png.is_empty() || png.len() > MAX_IMAGE_BYTES {
        engine.report_error("Clipboard image is empty or too large");
        return true;
    }
    if png.len() as u64 > clarp_core::attachments::MAX_UPLOAD_BYTES {
        engine.report_error("Could not store the pasted image (maximum 50 MB)");
        return true;
    }
    let Some(folder) = clarp_core::media::cache_dir().map(|dir| dir.join("clipboard-images")) else {
        engine.report_error("Could not store the pasted image");
        return true;
    };
    let path = folder.join(format!("{}.png", uuid::Uuid::new_v4()));
    let stored = std::fs::create_dir_all(&folder).and_then(|_| {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path)?.write_all(&png)
    });
    if let Err(error) = stored {
        eprintln!("clarp-slint: could not store a pasted image at {}: {error}", path.display());
        engine.report_error("Could not store the pasted image");
        return true;
    }
    engine.attach_file(session, &path);
    true
}
