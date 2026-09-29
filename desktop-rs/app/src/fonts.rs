//! Installed font families, read once from fontconfig, so a reading theme
//! degrades to the next listed family rather than the platform default.

use std::collections::HashSet;
use std::sync::OnceLock;

pub fn installed(family: &str) -> bool {
    static FAMILIES: OnceLock<HashSet<String>> = OnceLock::new();
    FAMILIES
        .get_or_init(|| match std::process::Command::new("fc-list").args([":", "family"]).output() {
            Ok(output) => String::from_utf8_lossy(&output.stdout)
                .lines()
                .flat_map(|line| line.split(',').map(|f| f.trim().to_lowercase()))
                .collect(),
            Err(error) => {
                eprintln!("fonts: fc-list unavailable ({error}); reading themes use generic families");
                HashSet::new()
            }
        })
        .contains(&family.to_lowercase())
}
