//! Small square pictures of a portrait, for the lists where an app shows a face in a circle.
//!
//! An app asks for one with `px` beside the address of a picture it already has
//! (`/avatars/<agent>?v=&px=128`, `/static/avatars/<file>?v=&px=128`): a square JPEG cut
//! from the middle of the picture. It is made once for a picture and a size, and kept in the
//! blob store under a name of its own, so it is never made again. A size not offered, or a
//! picture that cannot be read as one, is answered with the whole picture as before.

use std::io::Cursor;

use clarp_ports::blobs::Blobs;
use image::codecs::jpeg::JpegEncoder;
use image::imageops::FilterType;
use image::{DynamicImage, ImageReader, Limits, Rgb, RgbImage};

use crate::fleet::picture_name;

/// The sizes a thumbnail is made in, in pixels a side.
pub const SIZES: [u32; 3] = [64, 128, 256];

/// How finely a thumbnail is kept: about 10 KB at 128 pixels.
const QUALITY: u8 = 80;

/// The largest portrait a thumbnail is made from; a larger one is sent whole.
pub const SOURCE_MOST: u64 = 32 << 20;

/// The largest side and the most memory a portrait may take to be read.
const SIDE_MOST: u32 = 8192;
const MEMORY_MOST: u64 = 256 << 20;

/// The size asked for (`px`), when it is one a thumbnail is made in.
pub fn size(asked: Option<&str>) -> Option<u32> {
    let px = asked?.trim().parse::<u32>().ok()?;
    SIZES.contains(&px).then_some(px)
}

/// The name a thumbnail of the portrait with this digest is kept under.
pub fn name(digest: &str, px: u32) -> String {
    format!("{digest}.px{px}.jpg")
}

/// The name a thumbnail of a picture this Host ships is kept under, by what is in the file
/// (its version, sixteen signs of its SHA-256).
pub fn shipped_name(version: &str, px: u32) -> String {
    format!("{version}.shipped.px{px}.jpg")
}

/// The name the thumbnail of this portrait in this size is kept under, made and kept first
/// when it is not there yet. `None`: the portrait is not kept, is too large, or cannot be
/// read as a picture; whoever asked is sent the portrait whole.
pub fn kept(blobs: &dyn Blobs, digest: &str, px: u32) -> Option<String> {
    kept_or_made(blobs, &name(digest, px), px, || {
        let source = picture_name(digest);
        let length = blobs.len(&source).ok()??;
        if length > SOURCE_MOST {
            return None;
        }
        blobs.read(&source, 0, usize::try_from(length).ok()?).ok()
    })
}

/// The bytes of the thumbnail kept under `kept_as`, made first of the picture `source` reads
/// when it is not kept yet. `None`: there is no such picture, it is too large, or it cannot
/// be read as one.
pub fn kept_or_made(
    blobs: &dyn Blobs,
    kept_as: &str,
    px: u32,
    source: impl FnOnce() -> Option<Vec<u8>>,
) -> Option<String> {
    if matches!(blobs.len(kept_as), Ok(Some(length)) if length > 0) {
        return Some(kept_as.to_string());
    }
    let bytes = source()?;
    if bytes.is_empty() || bytes.len() as u64 > SOURCE_MOST {
        return None;
    }
    let made = made(&bytes, px)?;
    // Two asking at once may both make it: the store keeps the first and leaves it.
    blobs.put(kept_as, &made).ok()?;
    Some(kept_as.to_string())
}

/// What is kept under a name, whole. `None`: nothing is, or it cannot be read.
pub fn read_whole(blobs: &dyn Blobs, name: &str) -> Option<Vec<u8>> {
    let length = blobs.len(name).ok()??;
    blobs.read(name, 0, usize::try_from(length).ok()?).ok()
}

/// A square JPEG of at most `px` pixels a side, cut from the middle of a picture (JPEG,
/// PNG, WebP or GIF, its first frame). A picture smaller than that is not made larger.
/// What is see-through is laid on white.
pub fn made(picture: &[u8], px: u32) -> Option<Vec<u8>> {
    let mut reader = ImageReader::new(Cursor::new(picture))
        .with_guessed_format()
        .ok()?;
    let mut limits = Limits::default();
    limits.max_image_width = Some(SIDE_MOST);
    limits.max_image_height = Some(SIDE_MOST);
    limits.max_alloc = Some(MEMORY_MOST);
    reader.limits(limits);
    let picture = reader.decode().ok()?;
    let (width, height) = (picture.width(), picture.height());
    let side = width.min(height);
    if side == 0 {
        return None;
    }
    let square = picture.crop_imm((width - side) / 2, (height - side) / 2, side, side);
    let px = px.min(side);
    let small = if px == side {
        square
    } else {
        square.resize_exact(px, px, FilterType::Lanczos3)
    };
    let mut out = Vec::new();
    let encoder = JpegEncoder::new_with_quality(&mut out, QUALITY);
    DynamicImage::ImageRgb8(on_white(&small))
        .write_with_encoder(encoder)
        .ok()?;
    Some(out)
}

/// A picture without what is see-through in it: laid on white.
fn on_white(picture: &DynamicImage) -> RgbImage {
    if !picture.color().has_alpha() {
        return picture.to_rgb8();
    }
    let rgba = picture.to_rgba8();
    RgbImage::from_fn(rgba.width(), rgba.height(), |x, y| {
        let [r, g, b, a] = rgba.get_pixel(x, y).0;
        let over = |channel: u8| {
            let (channel, a) = (u32::from(channel), u32::from(a));
            ((channel * a + 255 * (255 - a) + 127) / 255) as u8
        };
        Rgb([over(r), over(g), over(b)])
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use image::{ImageFormat, RgbaImage};

    fn encoded(picture: DynamicImage, format: ImageFormat) -> Vec<u8> {
        let mut out = Cursor::new(Vec::new());
        picture.write_to(&mut out, format).unwrap();
        out.into_inner()
    }

    fn gradient(width: u32, height: u32) -> RgbaImage {
        RgbaImage::from_fn(width, height, |x, y| {
            image::Rgba([(x % 256) as u8, (y % 256) as u8, ((x + y) % 256) as u8, 255])
        })
    }

    fn read(jpeg: &[u8]) -> DynamicImage {
        assert!(jpeg.starts_with(b"\xff\xd8\xff"), "a thumbnail is a JPEG");
        image::load_from_memory_with_format(jpeg, ImageFormat::Jpeg).unwrap()
    }

    #[test]
    fn only_the_offered_sizes_are_made() {
        assert_eq!(size(Some("128")), Some(128));
        assert_eq!(size(Some("64")), Some(64));
        assert_eq!(size(Some("256")), Some(256));
        assert_eq!(size(Some("100")), None);
        assert_eq!(size(Some("")), None);
        assert_eq!(size(None), None);
    }

    #[test]
    fn a_png_and_a_jpeg_become_small_square_jpegs() {
        let png = encoded(
            DynamicImage::ImageRgba8(gradient(600, 400)),
            ImageFormat::Png,
        );
        let tall = DynamicImage::ImageRgba8(gradient(400, 900)).to_rgb8();
        let jpeg = encoded(DynamicImage::ImageRgb8(tall), ImageFormat::Jpeg);
        for source in [png, jpeg] {
            let small = read(&made(&source, 128).unwrap());
            assert_eq!((small.width(), small.height()), (128, 128));
        }
    }

    #[test]
    fn a_small_picture_is_not_made_larger() {
        let png = encoded(DynamicImage::ImageRgba8(gradient(50, 80)), ImageFormat::Png);
        let small = read(&made(&png, 256).unwrap());
        assert_eq!((small.width(), small.height()), (50, 50));
    }

    #[test]
    fn what_is_not_a_picture_is_not_made() {
        assert!(made(b"not a picture at all", 128).is_none());
        assert!(made(b"\x89PNG\r\n\x1a\nbroken", 128).is_none());
    }

    #[test]
    fn what_is_see_through_lies_on_white() {
        let clear = RgbaImage::from_pixel(32, 32, image::Rgba([0, 0, 0, 0]));
        let png = encoded(DynamicImage::ImageRgba8(clear), ImageFormat::Png);
        let small = read(&made(&png, 64).unwrap()).to_rgb8();
        assert!(small.get_pixel(16, 16).0.iter().all(|&c| c > 240));
    }
}
