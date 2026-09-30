use std::collections::HashMap;

use clarp_core::media::{avatar_url, mime, resolve_media_markdown, rounded_portrait};

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

#[test]
fn a_portrait_is_a_192px_circle() {
    let source = image::RgbaImage::from_pixel(300, 200, image::Rgba([200, 10, 10, 255]));
    let mut png = Vec::new();
    source.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png).unwrap();
    let portrait = image::load_from_memory(&rounded_portrait(&png).expect("portrait")).unwrap().to_rgba8();
    assert_eq!(portrait.dimensions(), (192, 192));
    assert_eq!(portrait.get_pixel(96, 96)[3], 255, "opaque centre");
    assert_eq!(portrait.get_pixel(0, 0)[3], 0, "transparent corner");
    assert_eq!(portrait.get_pixel(96, 96)[0], 200);
    assert_eq!(rounded_portrait(b"not an image"), None);
    // The circle's edge is antialiased (tst_native_core::circularPortraitsAreBoundedAndAntialiased).
    assert!((0..192).any(|x| (1..255).contains(&portrait.get_pixel(x, 20)[3])), "antialiased edge");
    // Formats are named from their magic bytes; JPEG and BMP must decode.
    let rgb = image::RgbImage::from_pixel(400, 240, image::Rgb([255, 0, 0]));
    for format in [image::ImageFormat::Jpeg, image::ImageFormat::Bmp] {
        let mut encoded = Vec::new();
        rgb.write_to(&mut std::io::Cursor::new(&mut encoded), format).unwrap();
        let portrait = image::load_from_memory(&rounded_portrait(&encoded).unwrap_or_else(|| panic!("{format:?} portrait"))).unwrap();
        assert_eq!((portrait.width(), portrait.height()), (192, 192), "{format:?}");
    }
}
