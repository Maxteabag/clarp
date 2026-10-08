//! Avatars and chat media (C++ `AppController` avatar/media helpers and
//! `PortraitImage`): which URL an agent's portrait comes from, the square
//! source cached from the Host and the round portraits made from it at the
//! device size they are drawn at, and `clarp-media://asset/<id>` links
//! resolved to locally cached files.

use std::collections::HashMap;
use std::sync::LazyLock;

use fancy_regex::{Captures, Regex};

pub const MAX_PORTRAIT_BYTES: usize = 20 * 1024 * 1024;
pub const MAX_INLINE_MEDIA_BYTES: usize = 20 * 1024 * 1024;
/// The largest portrait source kept: Host portraits are 512 px.
pub const PORTRAIT_SOURCE_MAX: u32 = 1024;

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
    Some(crate::dirs::cache_home()?.join("MaxTeaBag").join("ClarpRust"))
}

/// The cached portrait source (the Host's, cropped square) for a
/// Host-qualified avatar URL. Round portraits are made from it beside it
/// (`sized_portrait_path`); the 192 px rounded files of earlier versions in
/// `portraits/` are not read.
pub fn portrait_cache_path(cache: &std::path::Path, url: &str) -> std::path::PathBuf {
    use sha2::{Digest, Sha256};
    let key: String = Sha256::digest(url.as_bytes()).iter().take(12).map(|b| format!("{b:02x}")).collect();
    cache.join("portraits").join("sources").join(format!("{key}.png"))
}

/// The device pixels a portrait `logical` px wide covers at `scale` (the
/// monitor's times the reader's interface scale).
pub fn device_side(logical: f32, scale: f32) -> u32 {
    (logical * scale).round().max(1.0) as u32
}

/// The round portrait of `sources` (one agent, or a pair room's two) made
/// for `side` device pixels, cached beside the first source.
pub fn sized_portrait_path(sources: &[&std::path::Path], side: u32) -> std::path::PathBuf {
    let stems: Vec<String> = sources.iter().map(|p| p.file_stem().unwrap_or_default().to_string_lossy().into_owned()).collect();
    let folder = sources.first().and_then(|p| p.parent()).unwrap_or_else(|| std::path::Path::new("."));
    folder.join(format!("{}@{side}.png", stems.join("+")))
}

/// Whether `path` is a round portrait made for one size (not a source).
pub fn is_sized_portrait(path: &std::path::Path) -> bool {
    path.file_stem().is_some_and(|s| s.to_string_lossy().contains('@'))
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

/// RGBA pixels as PNG (a pasted clipboard image).
pub fn png_from_rgba(width: u32, height: u32, rgba: Vec<u8>) -> Option<Vec<u8>> {
    let image = image::RgbaImage::from_raw(width, height, rgba)?;
    let mut png = Vec::new();
    image.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png).ok()?;
    Some(png)
}

pub fn is_png(bytes: &[u8]) -> bool {
    bytes.starts_with(b"\x89PNG\r\n\x1a\n")
}

/// An icon for StatusNotifierItem hosts: `side` px square, ARGB32 in
/// network byte order.
pub fn argb_icon(png: &[u8], side: u32) -> Option<(i32, i32, Vec<u8>)> {
    let image = image::load_from_memory(png).ok()?;
    let rgba = image.resize_exact(side, side, image::imageops::FilterType::Triangle).to_rgba8();
    let data = rgba.pixels().flat_map(|p| [p[3], p[0], p[1], p[2]]).collect();
    Some((side as i32, side as i32, data))
}

/// The Host's portrait cropped to its centred square, as PNG, at its own
/// resolution (at most `PORTRAIT_SOURCE_MAX`): the UI makes each size it
/// draws from this. None when the bytes are not a readable image up to
/// 4096 px.
pub fn square_portrait(bytes: &[u8]) -> Option<Vec<u8>> {
    let format = format_of(bytes)?;
    let mut reader = image::ImageReader::with_format(std::io::Cursor::new(bytes), format);
    reader.no_limits();
    let (width, height) = reader.into_dimensions().ok()?;
    if width == 0 || height == 0 || width > 4096 || height > 4096 {
        return None;
    }
    // A square PNG small enough is kept as the Host sent it.
    if format == image::ImageFormat::Png && width == height && width <= PORTRAIT_SOURCE_MAX {
        return Some(bytes.to_vec());
    }
    let source = image::load_from_memory_with_format(bytes, format).ok()?;
    let side = width.min(height);
    let mut square = source.crop_imm((width - side) / 2, (height - side) / 2, side, side);
    if side > PORTRAIT_SOURCE_MAX {
        square = square.resize_exact(PORTRAIT_SOURCE_MAX, PORTRAIT_SOURCE_MAX, image::imageops::FilterType::Lanczos3);
    }
    png(&square.into_rgba8())
}

/// An image as PNG.
pub fn png(image: &image::RgbaImage) -> Option<Vec<u8>> {
    let mut png = Vec::new();
    image.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png).ok()?;
    Some(png)
}

/// `source`'s centred square at `side` px: Lanczos3 down (sharp, without
/// the moiré of a plain sample), Catmull-Rom up when the Host's own
/// portrait is smaller than the size it is drawn at. Through
/// `DynamicImage`, whose resizing is compiled (and optimised) in the image
/// crate even in debug builds; the generic `imageops` ones are not.
fn square_at(source: &image::RgbaImage, side: u32) -> image::RgbaImage {
    let (width, height) = source.dimensions();
    let small = width.min(height).max(1);
    let mut square = image::DynamicImage::ImageRgba8(source.clone());
    if width != height {
        square = square.crop_imm((width - small) / 2, (height - small) / 2, small, small);
    }
    if small == side {
        return square.into_rgba8();
    }
    let filter = if small > side { image::imageops::FilterType::Lanczos3 } else { image::imageops::FilterType::CatmullRom };
    square.resize_exact(side, side, filter).into_rgba8()
}

/// How much of the pixel at (`x`, `y`) lies inside a circle of `radius`
/// centred in a `side` px square: 4×4 samples per pixel, so the edge
/// fades over its pixel the way the shape actually crosses it.
fn coverage(side: u32, radius: f32, x: u32, y: u32) -> f32 {
    let centre = side as f32 / 2.0;
    let (dx, dy) = (x as f32 + 0.5 - centre, y as f32 + 0.5 - centre);
    let distance = (dx * dx + dy * dy).sqrt();
    // Wholly inside or outside: no need to sample.
    if distance <= radius - 0.75 {
        return 1.0;
    }
    if distance >= radius + 0.75 {
        return 0.0;
    }
    let mut inside = 0;
    for sy in 0..4 {
        for sx in 0..4 {
            let (px, py) = (x as f32 + (sx as f32 + 0.5) / 4.0 - centre, y as f32 + (sy as f32 + 0.5) / 4.0 - centre);
            if px * px + py * py <= radius * radius {
                inside += 1;
            }
        }
    }
    inside as f32 / 16.0
}

/// Cuts `image` (a square) to a circle with soft edges.
fn cut_round(image: &mut image::RgbaImage) {
    let side = image.width();
    let radius = side as f32 / 2.0;
    for (x, y, pixel) in image.enumerate_pixels_mut() {
        pixel[3] = (f32::from(pixel[3]) * coverage(side, radius, x, y)).round() as u8;
    }
}

/// The portrait to draw at `side` device pixels: downscaled from the
/// source and cut round with antialiased edges, so the software renderer
/// draws it pixel for pixel, with no scaling and no clip.
pub fn round_portrait(source: &image::RgbaImage, side: u32) -> image::RgbaImage {
    let mut portrait = square_at(source, side.max(1));
    cut_round(&mut portrait);
    portrait
}

/// A pair room's portrait: the middle of `left`'s portrait on the left,
/// of `right`'s on the right, in one soft-edged circle.
pub fn pair_portrait(left: &image::RgbaImage, right: &image::RgbaImage, side: u32) -> image::RgbaImage {
    let side = side.max(2);
    let (a, b) = (square_at(left, side), square_at(right, side));
    let half = side / 2;
    let from = side / 4;
    let mut pair = image::RgbaImage::from_fn(side, side, |x, y| {
        if x < half { *a.get_pixel(from + x, y) } else { *b.get_pixel((from + x - half).min(side - 1), y) }
    });
    cut_round(&mut pair);
    pair
}

/// A white ring `stroke` device pixels wide just inside a `side` px
/// square, with soft edges both sides: drawn coloured (`colorize`) as an
/// avatar's outline or its status ring, smooth in the software renderer.
pub fn ring_mask(side: u32, stroke: f32) -> image::RgbaImage {
    let side = side.max(1);
    let outer = side as f32 / 2.0;
    let inner = (outer - stroke.max(0.5)).max(0.0);
    image::RgbaImage::from_fn(side, side, |x, y| {
        let alpha = coverage(side, outer, x, y) - coverage(side, inner, x, y);
        image::Rgba([255, 255, 255, (alpha.clamp(0.0, 1.0) * 255.0).round() as u8])
    })
}
