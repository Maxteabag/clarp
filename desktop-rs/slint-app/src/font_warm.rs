//! Font fallback, resolved before the reader needs it (#855).
//!
//! The first time the window draws a character its font lacks (a chrome
//! mark like ▣ or ★, an emoji, Greek, CJK, Arabic), the text shaper walks
//! the system's fallback families in order, asking the font configuration
//! for each family's files and reading each file's character map, until one
//! covers it. On a desktop with a few thousand fonts that walk took about
//! half a second, with the window frozen, the first time such a character
//! appeared. Every later one was cheap: the families stay resolved.
//!
//! The window's font collection keeps the system's resolved families in a
//! store its clones share, so a clone can do that walk on its own thread:
//! at start this shapes a line holding every non-ASCII mark the interface
//! draws, common emoji, a character no font has (which walks every fallback
//! family) and a word in each common script. It also resolves each script's
//! fallback family and hands it to the window's collection. Afterwards the
//! window's own shaping finds every family already resolved. If the
//! window needs a family first, it waits only for that one family.

use std::borrow::Cow;
use std::time::Instant;

use i_slint_core::textlayout::sharedparley::{fontique, parley};
use parley::style::{FontFamily, FontFamilyName, FontWeight, GenericFamily};

/// The interface's own font: the shaper tries it before the fallbacks.
const INTERFACE_FAMILY: &str = "JetBrains Mono";

/// What the warm-up shapes: the marks the interface draws (`.slint` files
/// and the Rust strings they show), common emoji, U+10FFFD (no font maps it,
/// so the shaper asks every fallback family) and a word per script.
const SAMPLE: &str = concat!(
    "±¶·×øΔ–—’“”•…‹›←↑→↓↔↳⇧⇄−≡⊘⋯⌫━▏■▣▤▦▭▶▸▾◆◇○◌●◷★☐☑☰♥♪⚠✓✕⟳⬢❞ ",
    "😀👍✅🙂❤️🎉🔥🚀👀🤔😂🙏💡📎 ",
    "\u{10FFFD} ",
    "αβγ Ωμέγα Привет Ελληνικά Українська ",
    "日本語 中文 ひらがな カタカナ 한국어 ",
    "مرحبا שלום नमस्ते สวัสดี ",
);

/// Scripts whose fallback family is resolved for the window up front:
/// each costs a font-configuration match the first time it is drawn.
const SCRIPTS: &[[u8; 4]] = &[
    *b"Latn", *b"Grek", *b"Cyrl", *b"Hani", *b"Hira", *b"Kana", *b"Hang", *b"Arab", *b"Hebr", *b"Deva", *b"Thai",
];

/// Starts the warm-up on its own thread, from the window's font collection.
/// Call it on the UI thread once the window exists.
pub fn start(window: &slint::Window) {
    let Some(context) = i_slint_core::window::WindowInner::from_pub(window).try_context() else {
        return;
    };
    let fonts = context.font_context().borrow().inner.clone();
    let spawned = std::thread::Builder::new().name("font-warm".into()).spawn(move || warm(fonts));
    if let Err(error) = spawned {
        eprintln!("clarp-slint: cannot warm the font fallback: {error}");
    }
}

fn warm(mut fonts: parley::FontContext) {
    let started = Instant::now();
    let families = [
        FontFamilyName::named(INTERFACE_FAMILY),
        FontFamilyName::Generic(GenericFamily::SansSerif),
        FontFamilyName::Generic(GenericFamily::SystemUi),
    ];
    let mut layout = parley::LayoutContext::<()>::new();
    // Regular and bold: a family's bold face is a file of its own.
    for weight in [400.0, 700.0] {
        let mut builder = layout.ranged_builder(&mut fonts, SAMPLE, 1.0, false);
        builder.push_default(FontFamily::List(Cow::Borrowed(&families)));
        builder.push_default(parley::StyleProperty::FontSize(15.0));
        builder.push_default(parley::StyleProperty::FontWeight(FontWeight::new(weight)));
        drop(builder.build(SAMPLE));
    }
    let shaped = started.elapsed().as_millis();
    // A script's fallback stays in the collection that asked for it; set it
    // on the shared collection so the window's finds it too.
    for script in SCRIPTS {
        let key = fontique::FallbackKey::new(fontique::Script::from_bytes(*script), None);
        let resolved: Vec<fontique::FamilyId> = fonts.collection.fallback_families(key).collect();
        if !resolved.is_empty() {
            fonts.collection.set_fallbacks(key, resolved.into_iter());
        }
    }
    eprintln!(
        "clarp-slint: font fallback warmed in {} ms (shaping {shaped} ms)",
        started.elapsed().as_millis()
    );
}
