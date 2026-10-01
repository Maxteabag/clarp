//! Port of desktop/tests/tst_reading_theme.cpp.

use std::collections::HashSet;

use clarp_core::reading_theme::*;

fn s<'a>(theme: &'a serde_json::Map<String, serde_json::Value>, key: &str) -> &'a str {
    theme[key].as_str().unwrap()
}

#[test]
fn body_text_meets_aaa_on_every_surface() {
    for t in themes() {
        assert!(contrast(s(t, "text"), s(t, "background")) >= 7.0, "{}", s(t, "id"));
        assert!(contrast(s(t, "text"), s(t, "bubble")) >= 7.0, "{}", s(t, "id"));
        assert!(contrast(s(t, "selectedText"), s(t, "selection")) >= 4.5, "{}", s(t, "id"));
    }
}

#[test]
fn secondary_text_meets_aa() {
    for t in themes() {
        for role in ["mutedText", "faintText", "link"] {
            assert!(contrast(s(t, role), s(t, "background")) >= 4.5, "{} {role}", s(t, "id"));
        }
    }
}

#[test]
fn chrome_roles_stay_readable_on_every_theme() {
    for t in themes() {
        let id = s(t, "id");
        for surface in ["window", "raised", "control", "sunken"] {
            assert!(contrast(s(t, "chromeText"), s(t, surface)) >= 7.0, "{id} {surface}");
            assert!(contrast(s(t, "secondary"), s(t, surface)) >= 4.5, "{id} {surface}");
        }
        let window = s(t, "window");
        assert!(contrast(s(t, "mutedText"), window) >= 4.5, "{id}");
        assert!(contrast(s(t, "accent"), window) >= 3.0, "{id}");
        assert!(contrast(s(t, "accentText"), s(t, "accent")) >= 4.5, "{id}");
        assert!(contrast(s(t, "warning"), window) >= 4.5, "{id}");
        assert!(contrast(s(t, "danger"), window) >= 4.5, "{id}");
        assert!(contrast(s(t, "success"), window) >= 3.0, "{id}");
        assert!(contrast(s(t, "border"), window) >= 1.3, "{id}");
        assert_eq!(t["light"].as_bool().unwrap(), lightness(window) > 0.5, "{id}");
    }
    assert!(theme("paper")["light"].as_bool().unwrap());
    assert!(!theme("terminal")["light"].as_bool().unwrap());
    let paper = style("paper", |_| true);
    assert_eq!(paper["light"], true);
    assert_eq!(paper["window"], theme("paper")["window"]);
}

#[test]
fn no_pure_polarity_extremes() {
    for t in themes() {
        assert!(contrast(s(t, "text"), s(t, "background")) < 18.0);
        assert!(!["#ffffff", "#000000"].contains(&s(t, "text").to_lowercase().as_str()));
    }
}

#[test]
fn body_size_and_measure_stay_in_reading_range() {
    for t in themes() {
        let size = t["fontPixelSize"].as_f64().unwrap();
        assert!((15.0..=18.0).contains(&size));
        let characters = t["measure"].as_f64().unwrap() / (size * 0.5);
        assert!((60.0..=115.0).contains(&characters), "{}", s(t, "id"));
    }
}

#[test]
fn ids_are_unique_and_unknown_falls_back_to_terminal() {
    let mut ids = HashSet::new();
    for t in themes() {
        assert!(ids.insert(s(t, "id")));
        assert!(!t["fontFamilies"].as_array().unwrap().is_empty());
    }
    assert_eq!(normalized_theme_id("nope"), "terminal");
    assert_eq!(normalized_theme_id(""), "terminal");
    assert_eq!(normalized_theme_id("paper"), "paper");
    assert_eq!(default_theme_id(), "terminal");
}

#[test]
fn options_mirror_themes() {
    let options = options();
    assert_eq!(options.len(), themes().len());
    for (option, t) in options.iter().zip(themes()) {
        assert_eq!(option["id"], t["id"]);
        assert!(!option["label"].as_str().unwrap().is_empty());
        assert!(!option["detail"].as_str().unwrap().is_empty());
    }
}

#[test]
fn font_falls_back_to_the_next_installed_family() {
    let paper = theme("paper");
    assert_eq!(resolve_font(paper, |_| true), "Literata");
    assert_eq!(resolve_font(paper, |f| f == "Noto Serif"), "Noto Serif");
    assert_eq!(resolve_font(paper, |_| false), "serif");
    let styled = style("paper", |_| true);
    assert_eq!(styled["fontFamily"], "Literata");
    assert_eq!(styled["background"], paper["background"]);
    assert_eq!(styled["fontPixelSize"], 17);
    assert_eq!(style("x", |_| true)["id"], "terminal");
}
