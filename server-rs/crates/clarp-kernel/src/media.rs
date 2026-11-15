//! What an agent publishes for its user to see: an image, a recording, a document.
//!
//! The Host decides what a file is by its first bytes and not by what the agent calls it,
//! gives it a name that is safe to keep, measures an image, and answers with one row, which
//! is what the apps and the agent's tool get. Only kinds on a short list are taken: what a
//! phone can show or play, and nothing that runs.
//!
//! These are the Python Host's rules (`server/lib/media_store.py`), and they are held to it
//! by made-up files of every kind under the names and declared types an agent might give
//! (`tests/media_vectors.rs`). Where that module asks the machine's table of file types, the
//! part of Python's own built-in table that it can ask for is carried here.

use serde_json::{Value, json};

/// The largest file that is taken.
pub const MAX_BYTES: usize = 75 * 1024 * 1024;

/// The kinds of image that are taken.
pub const IMAGES: [&str; 6] = [
    "image/gif",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/webp",
];

/// The kinds of file that are taken beside images: what can be played or read on a phone.
pub const FILES: [&str; 13] = [
    "application/json",
    "application/pdf",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-m4a",
    "audio/x-wav",
    "text/csv",
    "text/markdown",
    "text/plain",
    "video/mp4",
    "video/quicktime",
    "video/webm",
];

/// The kinds that are text: taken when they are text indeed.
const TEXT: [&str; 3] = ["text/plain", "text/csv", "text/markdown"];

pub fn is_image(mime: &str) -> bool {
    IMAGES.contains(&mime)
}

/// Whether a file of this kind is taken at all.
pub fn is_taken(mime: &str) -> bool {
    is_image(mime) || FILES.contains(&mime)
}

/// A declared type without its parameters, in small letters.
fn primary_type(content_type: &str) -> String {
    content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

/// The extension Python's own table of file types has for a type.
fn extension_of_type(mime: &str) -> Option<&'static str> {
    Some(match mime {
        "application/json" => ".json",
        "application/octet-stream" => ".bin",
        "application/pdf" => ".pdf",
        "application/zip" => ".zip",
        "audio/mp4" => ".m4a",
        "audio/mpeg" => ".mp3",
        "audio/x-wav" => ".wav",
        "image/gif" => ".gif",
        "image/heic" => ".heic",
        "image/heif" => ".heif",
        "image/jpeg" => ".jpg",
        "image/png" => ".png",
        "image/svg+xml" => ".svg",
        "image/webp" => ".webp",
        "text/csv" => ".csv",
        "text/html" => ".html",
        "text/markdown" => ".md",
        "text/plain" => ".txt",
        "video/mp4" => ".mp4",
        "video/quicktime" => ".mov",
        "video/webm" => ".webm",
        _ => return None,
    })
}

/// The type Python's own table of file types has for an extension, whatever its case.
fn type_of_extension(extension: &str) -> Option<&'static str> {
    Some(match extension.to_lowercase().as_str() {
        ".bin" => "application/octet-stream",
        ".csv" => "text/csv",
        ".gif" => "image/gif",
        ".heic" => "image/heic",
        ".heif" => "image/heif",
        ".htm" | ".html" => "text/html",
        ".jpe" | ".jpeg" | ".jpg" => "image/jpeg",
        ".json" => "application/json",
        ".m4a" => "audio/mp4",
        ".markdown" | ".md" => "text/markdown",
        ".mov" => "video/quicktime",
        ".mp3" => "audio/mpeg",
        ".mp4" => "video/mp4",
        ".pdf" => "application/pdf",
        ".png" => "image/png",
        ".svg" => "image/svg+xml",
        ".txt" => "text/plain",
        ".wav" => "audio/x-wav",
        ".webm" => "video/webm",
        ".webp" => "image/webp",
        ".zip" => "application/zip",
        _ => return None,
    })
}

/// The last part of a path: what is behind its last separator, a `.` and what is empty left
/// out (Python's `PurePath(...).name`).
fn last_part(path: &str) -> &str {
    path.split('/')
        .rfind(|part| !part.is_empty() && *part != ".")
        .unwrap_or("")
}

/// A name's extension with its dot: from the last dot on, when that dot is neither the
/// name's first character nor its last (Python's `PurePath(...).suffix`).
fn suffix(name: &str) -> &str {
    let name = last_part(name);
    match name.rfind('.') {
        Some(dot) if dot > 0 && dot + 1 < name.len() => &name[dot..],
        _ => "",
    }
}

/// A name that is safe to keep a file under: the last part of what the agent called it,
/// letters, digits and a few signs only, no spaces, no leading dot, 128 characters at most.
/// A file that is called nothing is called after its kind.
pub fn safe_name(raw: &str, content_type: &str) -> String {
    let unixed = raw.replace('\\', "/");
    let base = last_part(&unixed).trim();
    let kept: String = base
        .chars()
        .filter(|c| c.is_alphanumeric() || "._- ()".contains(*c))
        .collect();
    let cleaned = kept.trim().replace(' ', "_");
    let cleaned = cleaned.trim_start_matches('.');
    if cleaned.is_empty() {
        let extension = extension_of_type(&primary_type(content_type)).unwrap_or(".bin");
        return format!("media{extension}");
    }
    cleaned.chars().take(128).collect()
}

/// What a file is: by its first bytes where they say it, else by what it was declared as,
/// when its bytes do not speak against that. A declared kind of file whose bytes are not
/// that kind is nothing that is taken.
pub fn detect_mime(blob: &[u8], content_type: &str, name: &str) -> String {
    let mut declared = primary_type(content_type);
    let says_nothing = matches!(
        declared.as_str(),
        "" | "application/octet-stream" | "text/x-markdown"
    );
    if says_nothing && matches!(suffix(name).to_lowercase().as_str(), ".md" | ".markdown") {
        declared = "text/markdown".to_string();
    }
    if blob.starts_with(b"\x89PNG\r\n\x1a\n") {
        return "image/png".to_string();
    }
    if blob.starts_with(b"\xff\xd8\xff") {
        return "image/jpeg".to_string();
    }
    if blob.starts_with(b"GIF87a") || blob.starts_with(b"GIF89a") {
        return "image/gif".to_string();
    }
    if blob.len() >= 12 && &blob[..4] == b"RIFF" && &blob[8..12] == b"WEBP" {
        return "image/webp".to_string();
    }
    if is_what_it_says(blob, &declared) {
        return declared;
    }
    if FILES.contains(&declared.as_str()) {
        return "application/octet-stream".to_string();
    }
    if is_image(&declared) {
        return declared;
    }
    if !declared.is_empty() && declared != "application/octet-stream" {
        return declared;
    }
    match type_of_extension(suffix(name)) {
        Some(guessed) if FILES.contains(&guessed) => "application/octet-stream".to_string(),
        Some(guessed) => guessed.to_string(),
        None => "application/octet-stream".to_string(),
    }
}

/// Whether a file's bytes are of the kind of file it was declared as.
fn is_what_it_says(blob: &[u8], declared: &str) -> bool {
    match declared {
        "application/pdf" => blob.starts_with(b"%PDF-"),
        "audio/wav" | "audio/x-wav" => {
            blob.len() >= 12 && &blob[..4] == b"RIFF" && &blob[8..12] == b"WAVE"
        }
        "audio/mpeg" => {
            blob.starts_with(b"ID3")
                || (blob.len() >= 2 && blob[0] == 0xFF && blob[1] & 0xE0 == 0xE0)
        }
        "video/webm" => blob.starts_with(b"\x1aE\xdf\xa3"),
        "audio/mp4" | "audio/x-m4a" | "video/mp4" | "video/quicktime" => {
            blob.len() >= 12 && &blob[4..8] == b"ftyp"
        }
        "application/json" => serde_json::from_slice::<Value>(blob).is_ok(),
        text if TEXT.contains(&text) => std::str::from_utf8(blob).is_ok(),
        _ => false,
    }
}

/// The extension a file is kept under: its name's own, in small letters, when that is
/// letters and digits; else the one of its kind.
pub fn extension(name: &str, mime: &str) -> String {
    let own = suffix(name).to_lowercase();
    if !own.is_empty() && own.chars().all(|c| c.is_alphanumeric() || c == '.') {
        return own.chars().take(12).collect();
    }
    match extension_of_type(mime) {
        Some(".jpe") => ".jpg".to_string(),
        Some(of_kind) => of_kind.to_string(),
        None => ".img".to_string(),
    }
}

fn be_u16(blob: &[u8], at: usize) -> Option<u16> {
    let bytes = blob.get(at..at + 2)?;
    Some(u16::from_be_bytes([bytes[0], bytes[1]]))
}

fn le_u16(blob: &[u8], at: usize) -> Option<u32> {
    let bytes = blob.get(at..at + 2)?;
    Some(u32::from(u16::from_le_bytes([bytes[0], bytes[1]])))
}

/// How wide and how high an image is, read from its header. Nothing for a kind whose
/// header is not read here, and for a file too short to have one.
pub fn image_size(blob: &[u8], mime: &str) -> (Option<u32>, Option<u32>) {
    let size = match mime {
        "image/png" if blob.len() >= 24 => {
            let word = |at: usize| {
                blob.get(at..at + 4)
                    .map(|b| u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
            };
            word(16).zip(word(20))
        }
        "image/gif" if blob.len() >= 10 => le_u16(blob, 6).zip(le_u16(blob, 8)),
        "image/jpeg" => jpeg_size(blob),
        "image/webp" if blob.len() >= 30 => webp_size(blob),
        _ => None,
    };
    (size.map(|(width, _)| width), size.map(|(_, height)| height))
}

/// A JPEG's size is in its first frame header, behind whatever segments come before it.
fn jpeg_size(blob: &[u8]) -> Option<(u32, u32)> {
    let mut at = 2;
    while at + 9 < blob.len() {
        if blob[at] != 0xFF {
            at += 1;
            continue;
        }
        let mut marker = blob[at + 1];
        at += 2;
        while marker == 0xFF && at < blob.len() {
            marker = blob[at];
            at += 1;
        }
        if matches!(marker, 0xD8 | 0xD9 | 0x01 | 0xD0..=0xD7) {
            continue;
        }
        let size = usize::from(be_u16(blob, at)?);
        if size < 2 || at + size > blob.len() {
            return None;
        }
        let frame = matches!(
            marker,
            0xC0..=0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF
        );
        if frame && at + 7 <= blob.len() {
            let height = be_u16(blob, at + 3)?;
            let width = be_u16(blob, at + 5)?;
            return Some((u32::from(width), u32::from(height)));
        }
        at += size;
    }
    None
}

/// A WebP says its size in one of three ways, by the kind of its first chunk.
fn webp_size(blob: &[u8]) -> Option<(u32, u32)> {
    match blob.get(12..16)? {
        b"VP8 " if blob.len() >= 30 => {
            Some((le_u16(blob, 26)? & 0x3FFF, le_u16(blob, 28)? & 0x3FFF))
        }
        b"VP8L" if blob.len() >= 25 => {
            let b: Vec<u32> = blob[21..25].iter().map(|byte| u32::from(*byte)).collect();
            let width = 1 + (((b[1] & 0x3F) << 8) | b[0]);
            let height = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6));
            Some((width, height))
        }
        b"VP8X" if blob.len() >= 30 => {
            let three = |at: usize| {
                u32::from(blob[at])
                    | (u32::from(blob[at + 1]) << 8)
                    | (u32::from(blob[at + 2]) << 16)
            };
            Some((1 + three(24), 1 + three(27)))
        }
        _ => None,
    }
}

/// A caption as the words inside the brackets of a Markdown image.
pub fn markdown_alt(text: &str) -> String {
    let text = if text.is_empty() { "image" } else { text };
    text.replace('[', "\\[")
        .replace(']', "\\]")
        .replace('\n', " ")
}

/// A published file, as the Host keeps it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Asset {
    pub asset_id: String,
    pub session: String,
    pub source_name: String,
    pub mime_type: String,
    pub bytes: u64,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub caption: String,
    pub created_by: String,
    pub created_at: i64,
}

impl Asset {
    /// The row the apps and the agent's tool are given. An image comes with the Markdown
    /// that shows it in a chat; anything else is linked, not shown.
    pub fn public(&self) -> Value {
        let named = [&self.caption, &self.source_name]
            .into_iter()
            .find(|text| !text.is_empty())
            .map_or("file", String::as_str);
        let markdown = if is_image(&self.mime_type) {
            format!(
                "![{}](clarp-media://asset/{})",
                markdown_alt(named),
                self.asset_id
            )
        } else {
            String::new()
        };
        json!({
            "asset_id": self.asset_id,
            "session": self.session,
            "source_name": self.source_name,
            "file_name": self.source_name,
            "mime_type": self.mime_type,
            "bytes": self.bytes,
            "size_bytes": self.bytes,
            "width": self.width,
            "height": self.height,
            "caption": self.caption,
            "created_by": if self.created_by.is_empty() { "agent" } else { &self.created_by },
            "created_at": self.created_at,
            "url": format!("/media/{}", self.asset_id),
            "uri": format!("clarp-media://asset/{}", self.asset_id),
            "markdown": markdown,
        })
    }
}
