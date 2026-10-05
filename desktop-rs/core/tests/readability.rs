//! The colour science behind the readability rules, checked against
//! published values, and the rules themselves on every bundled theme.

use clarp_core::readability::*;

fn near(a: f64, b: f64, tolerance: f64) -> bool {
    (a - b).abs() <= tolerance
}

#[test]
fn apca_matches_the_reference_implementation() {
    // The APCA-W3 0.0.98G-4g README's examples.
    assert!(near(apca("#888888", "#ffffff"), 63.06, 0.05), "{}", apca("#888888", "#ffffff"));
    assert!(near(apca("#ffffff", "#888888"), -68.54, 0.05), "{}", apca("#ffffff", "#888888"));
    assert!(near(apca("#000000", "#aaaaaa"), 58.15, 0.05), "{}", apca("#000000", "#aaaaaa"));
    assert!(near(apca("#aaaaaa", "#000000"), -56.24, 0.05), "{}", apca("#aaaaaa", "#000000"));
    assert_eq!(apca("#777777", "#777777"), 0.0);
}

#[test]
fn ciede2000_matches_sharma_wu_dalal() {
    // Their test data has Lab inputs; these are sRGB colours whose
    // differences are checked against an independent implementation
    // (colormath / colour-science, both 2000 formula).
    assert!(near(delta_e("#ff0000", "#ff0000"), 0.0, 1e-9));
    assert!(near(delta_e("#000000", "#ffffff"), 100.0, 0.01), "{}", delta_e("#000000", "#ffffff"));
    assert!(near(delta_e("#ff0000", "#00ff00"), 86.61, 0.1), "{}", delta_e("#ff0000", "#00ff00"));
    assert!(near(delta_e("#0000ff", "#ffff00"), 103.43, 0.1), "{}", delta_e("#0000ff", "#ffff00"));
}

#[test]
fn simulated_vision_keeps_greys_and_merges_red_and_green() {
    for vision in Vision::ALL {
        for grey in ["#000000", "#777777", "#ffffff"] {
            let seen = simulate(grey, vision);
            assert!(delta_e(&seen, grey) < 1.5, "{} {grey} -> {seen}", vision.name());
        }
    }
    let pure = delta_e("#d62728", "#2ca02c");
    let deutan = delta_e(&simulate("#d62728", Vision::Deuteranopia), &simulate("#2ca02c", Vision::Deuteranopia));
    assert!(deutan < pure / 2.0, "deuteranopia should pull red and green together: {deutan} vs {pure}");
}

#[test]
fn alpha_tints_blend_over_their_surface() {
    assert_eq!(over("#ffffff", 0.5, "#000000").unwrap(), "#808080");
    assert_eq!(over("#80ffffff", 1.0, "#000000").unwrap(), "#808080");
    assert_eq!(over("#123456", 0.0, "#abcdef").unwrap(), "#abcdef");
}

/// The themes Peter kept as they were when the rules arrived (2026-10-02).
/// They fail some rules (docs/readability.md lists them); every other
/// theme, and any new one, must pass.
const KEPT_AS_THEY_WERE: [&str; 4] = ["terminal", "paper", "dusk", "hyperlegible"];

#[test]
fn every_new_theme_meets_the_readability_rules() {
    let failures: Vec<String> = all_failures()
        .into_iter()
        .filter(|f| !KEPT_AS_THEY_WERE.iter().any(|id| f.starts_with(&format!("{id}:"))))
        .collect();
    assert!(failures.is_empty(), "{} readability failures:\n{}", failures.len(), failures.join("\n"));
}

#[test]
fn the_kept_themes_still_exist() {
    let ids: Vec<String> = clarp_core::reading_theme::themes().iter().filter_map(|t| t.get("id").and_then(|v| v.as_str()).map(str::to_owned)).collect();
    for id in KEPT_AS_THEY_WERE {
        assert!(ids.iter().any(|i| i == id), "{id} is no longer a theme: drop it from KEPT_AS_THEY_WERE");
    }
    for id in ["sepia", "graphite", "night", "contrast", "studio"] {
        assert!(ids.iter().any(|i| i == id), "the {id} theme is missing");
    }
}

/// WCAG 2 contrast ratio of two #rrggbb colours.
fn ratio(a: &str, b: &str) -> f64 {
    let lum = |h: &str| {
        let h = h.trim_start_matches('#');
        let channel = |i: usize| {
            let c = f64::from(u8::from_str_radix(&h[i..i + 2], 16).unwrap()) / 255.0;
            if c <= 0.039_28 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) }
        };
        0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4)
    };
    let (x, y) = (lum(a), lum(b));
    (x.max(y) + 0.05) / (x.min(y) + 0.05)
}

/// The focused pane's frame (the `focus` role, else the accent) must stand
/// out from the window AND from the unfocused frames' border (WCAG 1.4.11,
/// 3:1 for UI parts), so it is clear which pane has the keyboard.
#[test]
fn the_focused_frame_stands_out_from_the_others() {
    let mut failures = Vec::new();
    for theme in clarp_core::reading_theme::themes() {
        let get = |k: &str| theme.get(k).and_then(|v| v.as_str()).unwrap_or_default().to_owned();
        let id = get("id");
        if KEPT_AS_THEY_WERE.contains(&id.as_str()) {
            continue;
        }
        let focus = Some(get("focus")).filter(|f| !f.is_empty()).unwrap_or_else(|| get("accent"));
        for (other, against) in [("window", get("window")), ("border", get("border"))] {
            let r = ratio(&focus, &against);
            if r < 3.0 {
                failures.push(format!("{id}: focus {focus} vs {other} {against} is {r:.2}:1, needs 3:1"));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
