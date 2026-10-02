//! Prints every reading theme's readability audit as Markdown:
//! `cargo run -q -p clarp-core --example theme_audit [THEME...]`.

use clarp_core::readability::*;
use clarp_core::reading_theme::themes;

fn main() {
    let wanted: Vec<String> = std::env::args().skip(1).collect();
    for theme in themes() {
        let id = theme["id"].as_str().unwrap_or("?");
        if !wanted.is_empty() && !wanted.iter().any(|w| w == id) {
            continue;
        }
        let failures = failures(theme);
        println!("### {id} ({} failures)\n", failures.len());
        println!("| foreground | surface | colours | tier | needs | Lc | WCAG | |");
        println!("|---|---|---|---|---|---:|---:|---|");
        for m in measure(theme) {
            let (lc, ratio) = m.tier.minimum();
            println!(
                "| {} | {} | {} on {} | {} | Lc {lc} / {ratio}:1 | {:.1} | {:.2} | {} |",
                m.foreground, m.surface, m.fg, m.bg, m.tier.name(), m.lc.abs(), m.ratio, if m.passes() { "ok" } else { "**FAIL**" }
            );
        }
        println!("\nStatus colours under simulated colour vision (Machado 2009, severity 1.0), CIEDE2000 ≥ {MIN_STATUS_DELTA_E}:\n");
        println!("| vision | success | warning | danger | link | worst pair | ΔE00 |");
        println!("|---|---|---|---|---|---|---:|");
        for vision in Vision::ALL {
            let sim = |role: &str| theme[role].as_str().map(|c| simulate(c, vision)).unwrap_or_default();
            let (pair, de) = signal_separation(theme)
                .into_iter()
                .filter(|(v, _, _)| *v == vision)
                .map(|(_, p, d)| (p, d))
                .fold((("", ""), f64::MAX), |best, x| if x.1 < best.1 { x } else { best });
            println!(
                "| {} | {} | {} | {} | {} | {}/{} | {:.1}{} |",
                vision.name(), sim("success"), sim("warning"), sim("danger"), sim("link"), pair.0, pair.1, de,
                if de < MIN_STATUS_DELTA_E { " **FAIL**" } else { "" }
            );
        }
        if !failures.is_empty() {
            println!("\nFailures:\n");
            for f in &failures {
                println!("- {f}");
            }
        }
        println!();
    }
}
