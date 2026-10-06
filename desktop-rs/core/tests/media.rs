use std::collections::HashMap;

use clarp_core::media::{
    PORTRAIT_SOURCE_MAX, avatar_url, device_side, is_sized_portrait, mime, pair_portrait, portrait_cache_path, resolve_media_markdown, ring_mask,
    round_portrait, sized_portrait_path, square_portrait,
};

#[test]
fn portraits_come_from_the_agent_or_the_bundled_set() {
    assert_eq!(avatar_url("/media/x.png", "Rachel"), "/media/x.png");
    assert_eq!(avatar_url("", "Rachel"), "/static/avatars/rachel.png");
    assert_eq!(avatar_url("", "Nobody"), "");
    assert_eq!(mime("Image/PNG; charset=binary"), "image/png");
}

#[test]
fn media_links_resolve_to_cached_files() {
    let sources = HashMap::from([("a1".to_string(), "file:///cache/a1".to_string())]);
    let markdown = "![x](clarp-media://asset/a1) and ![y](clarp-media://asset/zz)";
    assert_eq!(resolve_media_markdown(markdown, &sources), "![x](file:///cache/a1) and ![y](clarp-media://asset/zz)");
}

fn png(image: &image::RgbaImage) -> Vec<u8> {
    let mut png = Vec::new();
    image.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png).unwrap();
    png
}

/// Alpha values strictly between transparent and opaque.
fn soft(image: &image::RgbaImage) -> usize {
    image.pixels().filter(|p| (1..255).contains(&p[3])).count()
}

#[test]
fn the_cached_source_is_the_hosts_portrait_cropped_square_at_full_resolution() {
    let source = image::RgbaImage::from_pixel(600, 400, image::Rgba([200, 10, 10, 255]));
    let square = image::load_from_memory(&square_portrait(&png(&source)).expect("square")).unwrap().to_rgba8();
    assert_eq!(square.dimensions(), (400, 400), "the centred square, not scaled down");
    assert_eq!(square.get_pixel(0, 0)[3], 255, "not rounded: the UI rounds at the size it draws");
    // A huge image is kept at a bounded size.
    let huge = image::RgbaImage::from_pixel(3000, 2000, image::Rgba([0, 0, 0, 255]));
    let square = image::load_from_memory(&square_portrait(&png(&huge)).unwrap()).unwrap();
    assert_eq!((square.width(), square.height()), (PORTRAIT_SOURCE_MAX, PORTRAIT_SOURCE_MAX));
    assert_eq!(square_portrait(b"not an image"), None);
    // Formats are named from their magic bytes; JPEG and BMP must decode.
    let rgb = image::RgbImage::from_pixel(400, 240, image::Rgb([255, 0, 0]));
    for format in [image::ImageFormat::Jpeg, image::ImageFormat::Bmp] {
        let mut encoded = Vec::new();
        rgb.write_to(&mut std::io::Cursor::new(&mut encoded), format).unwrap();
        let square = image::load_from_memory(&square_portrait(&encoded).unwrap_or_else(|| panic!("{format:?} portrait"))).unwrap();
        assert_eq!((square.width(), square.height()), (240, 240), "{format:?}");
    }
}

#[test]
fn a_portrait_is_rounded_at_the_device_size_it_is_drawn_with_soft_edges() {
    let source = image::RgbaImage::from_pixel(512, 512, image::Rgba([200, 10, 10, 255]));
    for side in [18, 32, 44, 51, 78] {
        let portrait = round_portrait(&source, side);
        assert_eq!(portrait.dimensions(), (side, side), "exactly the device size");
        let middle = side / 2;
        assert_eq!(portrait.get_pixel(middle, middle)[3], 255, "opaque centre at {side}");
        assert_eq!(portrait.get_pixel(middle, middle)[0], 200);
        assert_eq!(portrait.get_pixel(0, 0)[3], 0, "transparent corner at {side}");
        // The circle's edge has sub-pixel coverage, not 0 or 255 only:
        // about one soft pixel per edge pixel, all the way round.
        let soft = soft(&portrait);
        assert!(soft as f32 >= side as f32 * 2.0, "{soft} soft edge pixels at {side}");
        // And it is a circle: symmetric, the edge where it should be.
        for y in 0..side {
            for x in 0..side {
                assert_eq!(portrait.get_pixel(x, y)[3], portrait.get_pixel(side - 1 - x, y)[3], "mirrored at {side}: {x},{y}");
                assert_eq!(portrait.get_pixel(x, y)[3], portrait.get_pixel(y, x)[3], "transposed at {side}: {x},{y}");
            }
        }
    }
}

#[test]
fn a_large_portrait_is_downscaled_without_aliasing() {
    // One-pixel stripes: a naive (nearest) downscale keeps whole stripes
    // and comes out black or white; a proper filter averages them grey.
    let stripes = image::RgbaImage::from_fn(2048, 2048, |x, _| if x % 2 == 0 { image::Rgba([0, 0, 0, 255]) } else { image::Rgba([255, 255, 255, 255]) });
    let portrait = round_portrait(&stripes, 40);
    for x in 12..28 {
        let grey = portrait.get_pixel(x, 20)[0];
        assert!((100..=155).contains(&grey), "pixel {x} is {grey}, not an average of the stripes");
    }
    // Detail survives a downscale: a sharp half-black, half-white source
    // keeps a sharp step (one or two pixels), not a blurry ramp.
    let halves = image::RgbaImage::from_fn(512, 512, |x, _| if x < 256 { image::Rgba([0, 0, 0, 255]) } else { image::Rgba([255, 255, 255, 255]) });
    let portrait = round_portrait(&halves, 40);
    let ramp = (0..40).filter(|x| (20..235).contains(&portrait.get_pixel(*x, 20)[0])).count();
    assert!(ramp <= 2, "{ramp} pixels between black and white");
}

#[test]
fn a_pair_rooms_portrait_is_one_circle_with_each_agent_on_a_side() {
    let left = image::RgbaImage::from_pixel(300, 300, image::Rgba([255, 0, 0, 255]));
    let right = image::RgbaImage::from_pixel(64, 64, image::Rgba([0, 0, 255, 255]));
    let pair = pair_portrait(&left, &right, 44);
    assert_eq!(pair.dimensions(), (44, 44));
    assert_eq!(pair.get_pixel(10, 22).0, [255, 0, 0, 255]);
    assert_eq!(pair.get_pixel(34, 22).0, [0, 0, 255, 255]);
    assert_eq!(pair.get_pixel(0, 0)[3], 0);
    assert!(soft(&pair) >= 88, "soft edges");
}

#[test]
fn a_ring_is_a_soft_outline_with_a_clear_middle() {
    for (side, stroke) in [(44, 1.0), (50, 2.0), (75, 3.0), (18, 1.15)] {
        let ring = ring_mask(side, stroke);
        assert_eq!(ring.dimensions(), (side, side));
        assert_eq!(ring.get_pixel(side / 2, side / 2)[3], 0, "clear middle");
        assert_eq!(ring.get_pixel(0, 0)[3], 0, "clear corner");
        assert!(ring.pixels().all(|p| p[0] == 255 && p[1] == 255 && p[2] == 255), "white, for the theme to colour");
        assert!(ring.pixels().any(|p| p[3] > 200), "a visible line at {side}");
        assert!(soft(&ring) as f32 >= side as f32 * 2.0, "soft both sides at {side}");
    }
}

#[test]
fn sized_portraits_are_cached_per_device_size() {
    assert_eq!(device_side(44.0, 1.0), 44);
    assert_eq!(device_side(44.0, 1.15), 51);
    assert_eq!(device_side(32.0, 2.0), 64);
    let cache = std::path::Path::new("/cache");
    let source = portrait_cache_path(cache, "http://host/static/avatars/rachel.png");
    let other = portrait_cache_path(cache, "http://host/static/avatars/mike.png");
    assert_ne!(source, other);
    // The key holds the size and the scale it is drawn at (as device pixels).
    let keys = [
        sized_portrait_path(&[&source], device_side(44.0, 1.0)),
        sized_portrait_path(&[&source], device_side(44.0, 1.15)),
        sized_portrait_path(&[&source], device_side(32.0, 1.15)),
        sized_portrait_path(&[&source, &other], device_side(44.0, 1.0)),
        sized_portrait_path(&[&other, &source], device_side(44.0, 1.0)),
    ];
    for (i, a) in keys.iter().enumerate() {
        assert_eq!(a.parent(), source.parent(), "beside its source");
        for b in &keys[i + 1..] {
            assert_ne!(a, b);
        }
    }
    assert!(is_sized_portrait(&keys[0]) && !is_sized_portrait(&source));
}
