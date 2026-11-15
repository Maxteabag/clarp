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
//!
//! A published file is the agent's that published it: it is a record in that agent's log
//! ([`Asset`], in the agent's [`MediaBook`]), and its bytes are kept under a name made of what
//! they are ([`Asset::blob`]), so that the same bytes published twice are kept once.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
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

/// Why a file is not taken.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refused {
    /// It is empty, or larger than [`MAX_BYTES`].
    Size,
    /// It is of a kind that is not on the list.
    Kind,
}

impl Refused {
    /// The Python Host's words for it.
    pub fn words(self) -> &'static str {
        match self {
            Self::Size => "bad size",
            Self::Kind => "unsupported media type",
        }
    }
}

/// What a file that is taken was read as.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reading {
    /// The name it is kept under.
    pub name: String,
    pub mime: String,
    /// The extension its bytes are kept under, with its dot.
    pub extension: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

/// Reads a file an agent hands in under a name and a declared type: whether it is taken,
/// and as what.
pub fn read(blob: &[u8], name: &str, content_type: &str) -> Result<Reading, Refused> {
    if blob.is_empty() || blob.len() > MAX_BYTES {
        return Err(Refused::Size);
    }
    let name = safe_name(name, content_type);
    let mime = detect_mime(blob, content_type, &name);
    if !is_taken(&mime) {
        return Err(Refused::Kind);
    }
    let extension = extension(&name, &mime);
    let (width, height) = image_size(blob, &mime);
    Ok(Reading {
        name,
        mime,
        extension,
        width,
        height,
    })
}

/// A caption as it is kept: without the space around it, 500 characters at most.
pub fn kept_caption(caption: &str) -> String {
    caption.trim().chars().take(500).collect()
}

/// Who published a file, as it is kept: 80 characters at most, and `agent` when nobody is
/// named.
pub fn kept_publisher(created_by: &str) -> String {
    let named: String = created_by.trim().chars().take(80).collect();
    if named.is_empty() {
        "agent".to_string()
    } else {
        named
    }
}

/// The value of a header as it was meant. The agent's tool sends a name and a caption with
/// everything but letters, digits and a few signs written as `%XX`; what is no such sign
/// stays as it is, and bytes that are no text become the sign for that (Python's
/// `urllib.parse.unquote`).
pub fn unquoted(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut run: Vec<u8> = Vec::new();
    // A run of plain signs is read as a whole: the bytes of one character may be spread
    // over several `%XX`.
    let close = |run: &mut Vec<u8>, out: &mut String| {
        if !run.is_empty() {
            out.push_str(&String::from_utf8_lossy(&unescaped(run)));
            run.clear();
        }
    };
    for character in text.chars() {
        if character.is_ascii() {
            run.push(character as u8);
        } else {
            close(&mut run, &mut out);
            out.push(character);
        }
    }
    close(&mut run, &mut out);
    out
}

/// Plain signs with every `%XX` among them as the byte it stands for.
fn unescaped(run: &[u8]) -> Vec<u8> {
    let digit = |at: usize| run.get(at).and_then(|byte| char::from(*byte).to_digit(16));
    let mut out = Vec::with_capacity(run.len());
    let mut at = 0;
    while at < run.len() {
        let escaped = if run[at] == b'%' {
            digit(at + 1).zip(digit(at + 2))
        } else {
            None
        };
        match escaped {
            Some((high, low)) => {
                out.push((high * 16 + low) as u8);
                at += 3;
            }
            None => {
                out.push(run[at]);
                at += 1;
            }
        }
    }
    out
}

/// How many images a list shows: what was asked for, between 1 and 500, and 100 when
/// nothing was said, or nothing that is a number.
pub fn page_size(asked: Option<&str>) -> usize {
    let asked = asked.unwrap_or("").trim();
    let number = match asked.parse::<i64>() {
        Ok(0) | Err(_) => 100,
        Ok(number) => number,
    };
    usize::try_from(number.clamp(1, 500)).unwrap_or(100)
}

/// The part of a file a request asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part {
    /// All of it: no part was asked for, or none that can be read as one.
    Whole,
    /// From one byte to another, both included.
    Bytes { first: u64, last: u64 },
    /// A part the file does not have.
    Missing,
}

/// A run of digits without the zeros in front of it.
fn significant(digits: &str) -> &str {
    digits.trim_start_matches('0')
}

/// Whether one run of digits is a smaller number than another, however long they are.
fn smaller(a: &str, b: &str) -> bool {
    let (a, b) = (significant(a), significant(b));
    (a.len(), a) < (b.len(), b)
}

/// Reads a `Range` header against a file of `total` bytes, as the Python Host does
/// (`server.py`, `_byte_range`): only bytes, only the first of several ranges, and a range
/// that cannot be read as one is no range.
pub fn byte_range(header: &str, total: u64) -> Part {
    let Some(ranges) = header.strip_prefix("bytes=") else {
        return Part::Whole;
    };
    let first = ranges.split(',').next().unwrap_or("");
    let Some((low, high)) = first.split_once('-') else {
        return Part::Whole;
    };
    let blank: &[char] = &[' ', '\t'];
    let (low, high) = (low.trim_matches(blank), high.trim_matches(blank));
    let digits = |text: &str| text.len() <= 4000 && text.bytes().all(|byte| byte.is_ascii_digit());
    if !digits(low) || !digits(high) || (low.is_empty() && high.is_empty()) {
        return Part::Whole;
    }
    // A number no file is as long as (`None`) is larger than this one's length.
    let number = |text: &str| match significant(text) {
        "" => Some(0),
        digits => digits.parse::<u64>().ok(),
    };
    if low.is_empty() {
        // The last so many bytes.
        return match number(high) {
            Some(0) => Part::Missing,
            _ if total == 0 => Part::Missing,
            Some(last) => Part::Bytes {
                first: total.saturating_sub(last),
                last: total - 1,
            },
            None => Part::Bytes {
                first: 0,
                last: total - 1,
            },
        };
    }
    if !high.is_empty() && smaller(high, low) {
        return Part::Whole;
    }
    match number(low) {
        Some(first) if first < total => Part::Bytes {
            first,
            last: match number(high) {
                Some(last) if !high.is_empty() => last.min(total - 1),
                _ => total - 1,
            },
        },
        _ => Part::Missing,
    }
}

/// A published file, as the Host keeps it: one item of the log of the agent that published
/// it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Asset {
    pub asset_id: String,
    pub session: String,
    pub source_name: String,
    /// What its bytes are, as a number that only these bytes have.
    pub sha256: String,
    /// The extension its bytes are kept under, with its dot.
    pub extension: String,
    pub mime_type: String,
    pub bytes: u64,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub caption: String,
    pub created_by: String,
    pub created_at: i64,
    /// It came along with an agent that moved here, because the agent's conversation or
    /// library names it, and the Host it came from did not say it is the agent's own: a
    /// picture another agent published, most often. It is kept and served under its id like
    /// any other. It is not in the agent's gallery, and no artifact of the agent's is made
    /// of it.
    #[serde(default, skip_serializing_if = "is_false")]
    pub named_only: bool,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(truth: &bool) -> bool {
    !*truth
}

/// The name a file's bytes are kept under: what they are, and the extension of their kind.
/// The extension is for a person who looks at the disk; one that is not plain letters and
/// digits is left off, since a store takes plain names only.
pub fn blob_name(sha256: &str, extension: &str) -> String {
    if extension.is_ascii() {
        format!("{sha256}{extension}")
    } else {
        sha256.to_string()
    }
}

impl Asset {
    /// The name its bytes are kept under.
    pub fn blob(&self) -> String {
        blob_name(&self.sha256, &self.extension)
    }

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

/// The files an agent published, in the order it published them.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct MediaBook {
    assets: Vec<Asset>,
    places: HashMap<String, usize>,
}

impl MediaBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes a published file as the log has it. One that is there already is replaced.
    pub fn apply(&mut self, asset: &Asset) {
        match self.places.get(&asset.asset_id) {
            Some(&place) => self.assets[place] = asset.clone(),
            None => {
                self.places
                    .insert(asset.asset_id.clone(), self.assets.len());
                self.assets.push(asset.clone());
            }
        }
    }

    /// A file the book has under this id, the agent's own or one that came along with it.
    pub fn get(&self, asset_id: &str) -> Option<&Asset> {
        self.places.get(asset_id).map(|&place| &self.assets[place])
    }

    /// A file the agent published itself: what an artifact of its may be made of.
    pub fn own(&self, asset_id: &str) -> Option<&Asset> {
        self.get(asset_id).filter(|asset| !asset.named_only)
    }

    /// The agent's own images, the newest first, `limit` at most: its gallery.
    pub fn images(&self, limit: usize) -> Vec<&Asset> {
        let mut images: Vec<&Asset> = self
            .assets
            .iter()
            .filter(|asset| is_image(&asset.mime_type) && !asset.named_only)
            .collect();
        // Of two published in the same instant, the one published first comes first.
        images.sort_by_key(|asset| std::cmp::Reverse(asset.created_at));
        images.truncate(limit);
        images
    }

    pub fn len(&self) -> usize {
        self.assets.len()
    }

    pub fn is_empty(&self) -> bool {
        self.assets.is_empty()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn asset(id: &str, mime: &str, at: i64) -> Asset {
        Asset {
            asset_id: id.to_string(),
            session: "claire".to_string(),
            source_name: format!("{id}.bin"),
            sha256: "ab".repeat(32),
            extension: ".png".to_string(),
            mime_type: mime.to_string(),
            bytes: 3,
            width: None,
            height: None,
            caption: String::new(),
            created_by: "agent".to_string(),
            created_at: at,
            named_only: false,
        }
    }

    #[test]
    fn a_gallery_is_the_agents_images_newest_first() {
        let mut book = MediaBook::new();
        assert!(book.is_empty());
        for (id, mime, at) in [
            ("one", "image/png", 10),
            ("two", "application/pdf", 20),
            ("three", "image/gif", 30),
            ("four", "image/jpeg", 30),
            ("five", "image/webp", 5),
        ] {
            book.apply(&asset(id, mime, at));
        }
        fn ids(book: &MediaBook, limit: usize) -> Vec<&str> {
            book.images(limit)
                .iter()
                .map(|asset| asset.asset_id.as_str())
                .collect()
        }
        assert_eq!(ids(&book, 500), ["three", "four", "one", "five"]);
        assert_eq!(ids(&book, 2), ["three", "four"]);
        assert_eq!(book.len(), 5);
        assert_eq!(book.get("two").unwrap().mime_type, "application/pdf");
        assert!(book.get("six").is_none());
        // What the log says of a file again takes the place of what it said before.
        book.apply(&asset("one", "image/png", 40));
        assert_eq!(ids(&book, 1), ["one"]);
        assert_eq!(book.len(), 5);
    }

    #[test]
    fn a_file_that_came_along_is_kept_and_is_not_the_agents_own() {
        let mut book = MediaBook::new();
        book.apply(&asset("mine", "image/png", 10));
        let came = Asset {
            named_only: true,
            ..asset("theirs", "image/png", 20)
        };
        book.apply(&came);
        // It is there under its id, for whoever fetches it.
        assert_eq!(book.get("theirs"), Some(&came));
        assert_eq!(book.len(), 2);
        // It is not in the gallery, and nothing of the agent's is made of it.
        let gallery: Vec<&str> = book
            .images(500)
            .iter()
            .map(|asset| asset.asset_id.as_str())
            .collect();
        assert_eq!(gallery, ["mine"]);
        assert!(book.own("theirs").is_none());
        assert!(book.own("mine").is_some());
        // The log says it of the files it is true of, and of no other: what was written
        // before the Host knew of such files reads as the agent's own.
        let written = serde_json::to_value(&came).unwrap();
        assert_eq!(written["named_only"], true);
        let own = serde_json::to_value(asset("mine", "image/png", 10)).unwrap();
        assert!(own.get("named_only").is_none());
        let read: Asset = serde_json::from_value(own).unwrap();
        assert!(!read.named_only);
        // When the agent is found to have published it itself, it is its own from then on.
        book.apply(&asset("theirs", "image/png", 20));
        assert!(book.own("theirs").is_some());
        assert_eq!(book.images(500).len(), 2);
    }

    #[test]
    fn a_published_file_is_written_into_the_log_and_read_back_the_same() {
        let published = asset("asset_x", "image/png", 1_700_000_000_000);
        let written = serde_json::to_vec(&published).unwrap();
        assert_eq!(
            serde_json::from_slice::<Asset>(&written).unwrap(),
            published
        );
        assert_eq!(published.blob(), format!("{}.png", "ab".repeat(32)));
        // An extension in another script is the file's own, and no part of a plain name.
        assert_eq!(blob_name("abc", ".jpég"), "abc");
        assert_eq!(blob_name("abc", ""), "abc");
    }

    #[test]
    fn a_file_that_is_empty_or_too_large_or_of_no_kind_that_is_taken_is_refused() {
        assert_eq!(read(b"", "a.png", "image/png"), Err(Refused::Size));
        assert_eq!(
            read(b"#!/bin/sh\necho\n", "run.sh", "application/x-sh"),
            Err(Refused::Kind)
        );
        let taken = read(b"GIF89a\x02\x00\x03\x00rest", "../My shot.GIF", "").unwrap();
        assert_eq!(
            (
                taken.name.as_str(),
                taken.mime.as_str(),
                taken.extension.as_str()
            ),
            ("My_shot.GIF", "image/gif", ".gif")
        );
        assert_eq!((taken.width, taken.height), (Some(2), Some(3)));
        assert_eq!(Refused::Size.words(), "bad size");
        assert_eq!(Refused::Kind.words(), "unsupported media type");
    }

    #[test]
    fn a_caption_and_a_publisher_are_kept_short() {
        assert_eq!(kept_caption("  A chart \n"), "A chart");
        assert_eq!(kept_caption(&"é".repeat(600)).chars().count(), 500);
        assert_eq!(kept_publisher("  "), "agent");
        assert_eq!(kept_publisher(" oracle "), "oracle");
        assert_eq!(kept_publisher(&"x".repeat(90)).len(), 80);
    }

    #[test]
    fn a_list_shows_as_many_images_as_were_asked_for_within_bounds() {
        for (asked, shown) in [
            (None, 100),
            (Some(""), 100),
            (Some("7"), 7),
            (Some(" 7 "), 7),
            (Some("0"), 100),
            (Some("-3"), 1),
            (Some("501"), 500),
            (Some("many"), 100),
            (Some("1.5"), 100),
        ] {
            assert_eq!(page_size(asked), shown, "{asked:?}");
        }
    }
}
