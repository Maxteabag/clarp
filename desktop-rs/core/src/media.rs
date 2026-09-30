//! Avatars and chat media (C++ `AppController` avatar/media helpers and
//! `PortraitImage`): which URL an agent's portrait comes from, the rounded
//! 192 px portrait the sidebar shows, and `clarp-media://asset/<id>` links
//! resolved to locally cached files.

use std::collections::HashMap;
use std::sync::LazyLock;

use fancy_regex::{Captures, Regex};

pub const MAX_PORTRAIT_BYTES: usize = 20 * 1024 * 1024;
pub const MAX_INLINE_MEDIA_BYTES: usize = 20 * 1024 * 1024;
pub const PORTRAIT_SIDE: u32 = 192;

/// Names with a portrait bundled on every Host.
const BUNDLED: &[&str] = &[
    "adam", "antoni", "archie", "arnold", "bella", "bronte", "caleb", "diego", "domi", "elli", "freya", "imogen", "jasper",
    "josh", "kira", "lena", "marcus", "mike", "nadia", "omar", "pearl", "priya", "rachel", "ronan", "sam", "siobhan", "theo",
    "tristan", "wyatt", "yuki",
];

/// The agent's own avatar, else the Host's bundled portrait for its name.
pub fn avatar_url(avatar_url: &str, display_name: &str) -> String {
    if !avatar_url.is_empty() {
        return avatar_url.to_owned();
    }
    let slug: String = display_name.to_lowercase().chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').collect();
    if BUNDLED.contains(&slug.as_str()) { format!("/static/avatars/{slug}.png") } else { String::new() }
}

/// Where the Rust client caches things (portraits, chat images), apart
/// from the C++ client's cache.
pub fn cache_dir() -> Option<std::path::PathBuf> {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .filter(|v| !v.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".cache")))?;
    Some(base.join("MaxTeaBag").join("ClarpRust"))
}

/// The cached rounded portrait for a Host-qualified avatar URL.
pub fn portrait_cache_path(cache: &std::path::Path, url: &str) -> std::path::PathBuf {
    use sha2::{Digest, Sha256};
    let key: String = Sha256::digest(url.as_bytes()).iter().take(12).map(|b| format!("{b:02x}")).collect();
    cache.join("portraits").join(format!("{key}.png"))
}

/// The file a chat image is cached in: named by Host and asset, so assets
/// of different Hosts never collide.
pub fn media_file_name(base_url: &str, asset_id: &str) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(format!("{base_url}\0{asset_id}").as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// The MIME type of a Content-Type header, lowercased, without parameters.
pub fn mime(content_type: &str) -> String {
    content_type.split(';').next().unwrap_or_default().trim().to_lowercase()
}

/// Replaces `clarp-media://asset/<id>` with the cached file for each asset
/// that has one; unknown assets stay as they are.
pub fn resolve_media_markdown(markdown: &str, sources: &HashMap<String, String>) -> String {
    static MEDIA: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"clarp-media://asset/([A-Za-z0-9_-]+)").expect("valid"));
    MEDIA
        .replace_all(markdown, |caps: &Captures| sources.get(&caps[1]).cloned().unwrap_or_else(|| caps[0].to_owned()))
        .into_owned()
}

fn format_of(bytes: &[u8]) -> Option<image::ImageFormat> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some(image::ImageFormat::Png)
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        Some(image::ImageFormat::Jpeg)
    } else if bytes.starts_with(b"GIF8") {
        Some(image::ImageFormat::Gif)
    } else if bytes.len() >= 12 && bytes.starts_with(b"RIFF") && &bytes[8..12] == b"WEBP" {
        Some(image::ImageFormat::WebP)
    } else if bytes.starts_with(b"BM") {
        Some(image::ImageFormat::Bmp)
    } else {
        None
    }
}

/// The centred square of the image, scaled to 192 px and cut to a circle,
/// as PNG. None when the bytes are not a readable image up to 4096 px.
pub fn rounded_portrait(bytes: &[u8]) -> Option<Vec<u8>> {
    let format = format_of(bytes)?;
    let mut reader = image::ImageReader::with_format(std::io::Cursor::new(bytes), format);
    reader.no_limits();
    let (width, height) = reader.into_dimensions().ok()?;
    if width == 0 || height == 0 || width > 4096 || height > 4096 {
        return None;
    }
    let source = image::load_from_memory_with_format(bytes, format).ok()?;
    let side = width.min(height);
    let square = source.crop_imm((width - side) / 2, (height - side) / 2, side, side);
    let mut portrait = square.resize_exact(PORTRAIT_SIDE, PORTRAIT_SIDE, image::imageops::FilterType::Triangle).to_rgba8();
    // An antialiased circle: coverage by distance from the centre.
    let radius = PORTRAIT_SIDE as f32 / 2.0;
    for (x, y, pixel) in portrait.enumerate_pixels_mut() {
        let (dx, dy) = (x as f32 + 0.5 - radius, y as f32 + 0.5 - radius);
        let coverage = (radius - (dx * dx + dy * dy).sqrt() + 0.5).clamp(0.0, 1.0);
        pixel[3] = (f32::from(pixel[3]) * coverage).round() as u8;
    }
    let mut png = Vec::new();
    portrait.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png).ok()?;
    Some(png)
}

/// An icon for StatusNotifierItem hosts: `side` px square, ARGB32 in
/// network byte order.
pub fn argb_icon(png: &[u8], side: u32) -> Option<(i32, i32, Vec<u8>)> {
    let image = image::load_from_memory(png).ok()?;
    let rgba = image.resize_exact(side, side, image::imageops::FilterType::Triangle).to_rgba8();
    let data = rgba.pixels().flat_map(|p| [p[3], p[0], p[1], p[2]]).collect();
    Some((side as i32, side as i32, data))
}
