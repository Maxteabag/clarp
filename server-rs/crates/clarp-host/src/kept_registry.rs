//! The registry of what the Host keeps: every kind, each declared once beside its desk (its
//! `KEPT`), walked at every start before the door opens (`kept.rs`).

/// Every desk's kinds.
pub static REGISTRY: &[&[crate::kept::Kind]] = &[
    crate::kept::KEPT,
    crate::fleet::KEPT,
    crate::fleet::computer_jobs::KEPT,
    crate::fleet::portrait_jobs::KEPT,
    crate::voices::KEPT,
    crate::calls::KEPT,
    crate::calls::opening::KEPT,
    crate::calls::tools::KEPT,
    crate::android_chip::KEPT,
    crate::audio::KEPT,
    crate::chip::KEPT,
    crate::edge::KEPT,
    crate::controller_actions::KEPT,
    crate::usage::KEPT,
    crate::explain::KEPT,
    crate::heartbeat::KEPT,
    crate::janitors::KEPT,
    crate::keeper::KEPT,
    crate::label_checker::KEPT,
    crate::model_fallbacks::KEPT,
    crate::oracle::delegations::KINDS,
    crate::oracle::handoffs::KINDS,
    crate::oracle::memory::KEPT,
    crate::places::KEPT,
    crate::quota::KEPT,
    crate::quota_keeper::KEPT,
    crate::router::KEPT,
    crate::router::phrases::KEPT,
    crate::schedules::KEPT,
    crate::seats::KINDS,
    crate::heard::inventory::KEPT,
    crate::heard::install::KEPT,
    crate::turn_identity::KEPT,
    crate::vocabulary::store::KEPT,
    crate::library::events::KEPT,
];

/// Every kind the Host keeps.
pub fn kinds() -> impl Iterator<Item = &'static crate::kept::Kind> {
    REGISTRY.iter().flat_map(|kinds| kinds.iter())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::collections::{BTreeMap, BTreeSet};
    use std::path::Path;

    use regex::Regex;

    /// Every Rust file of the Host's own code, without its tests.
    fn sources(dir: &Path, found: &mut Vec<(String, String)>) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            if path.is_dir() {
                sources(&path, found);
            } else if name.ends_with(".rs") && !name.ends_with("_tests.rs") && name != "tests.rs" {
                found.push((
                    path.display().to_string(),
                    std::fs::read_to_string(&path).unwrap(),
                ));
            }
        }
    }

    // What these scans see, and no more: a kind named at the call by a constant of the same file
    // or by a string literal. A table named by a variable, by a constant of another file, or read
    // through a helper that takes the kind as an argument is not seen by them: those are held by
    // the desks' own reviews.

    #[test]
    fn no_kind_is_kept_or_read_that_the_registry_does_not_have() {
        let registered: BTreeSet<&str> = super::kinds().map(|kind| kind.name).collect();
        let mut found = Vec::new();
        sources(
            &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
            &mut found,
        );
        let constant =
            Regex::new(r#"const ([A-Z_][A-Z0-9_]*): &(?:'static )?str = "([^"]+)""#).unwrap();
        let used = Regex::new(
            r#"\.(kept|kept_all|kept_rows|keep|keep_together)\s*(?:::<[^>]*>)?\(\s*([A-Za-z_][\w:]*|"[^"]+")"#,
        )
        .unwrap();
        let shelved = Regex::new(r#"shelf\("([^"]+)"\)"#).unwrap();
        let mut missing: BTreeMap<String, String> = BTreeMap::new();
        for (file, text) in &found {
            // What the test of the log keeps for itself is no kind of the Host's.
            if file.ends_with("unkept.rs") {
                continue;
            }
            let local: BTreeMap<&str, &str> = constant
                .captures_iter(text)
                .map(|c| (c.get(1).unwrap().as_str(), c.get(2).unwrap().as_str()))
                .collect();
            let mut names: Vec<&str> = used
                .captures_iter(text)
                .filter_map(|c| {
                    let given = c.get(2).unwrap().as_str();
                    match given.strip_prefix('"') {
                        Some(literal) => literal.strip_suffix('"'),
                        None => local.get(given.rsplit("::").next().unwrap()).copied(),
                    }
                })
                .collect();
            names.extend(
                shelved
                    .captures_iter(text)
                    .map(|c| c.get(1).unwrap().as_str()),
            );
            for name in names {
                if !registered.contains(name) {
                    missing.insert(name.to_string(), file.clone());
                }
            }
        }
        assert!(
            missing.is_empty(),
            "kept and not in the registry: {missing:?}"
        );
    }

    #[test]
    fn a_kind_judged_when_read_is_read_nowhere_raw() {
        use crate::kept::Judged;
        let when_read: BTreeSet<&str> = super::kinds()
            .filter(|kind| kind.judged == Judged::WhenRead)
            .map(|kind| kind.name)
            .collect();
        let at_start: BTreeSet<&str> = super::kinds()
            .filter(|kind| kind.judged == Judged::AtStart)
            .map(|kind| kind.name)
            .collect();
        assert!(!when_read.is_empty());
        let mut found = Vec::new();
        sources(
            &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
            &mut found,
        );
        let constant =
            Regex::new(r#"const ([A-Z_][A-Z0-9_]*): &(?:'static )?str = "([^"]+)""#).unwrap();
        // A raw read: one record as it is, every record of a table, or a table's list.
        let raw = Regex::new(
            r#"\.(kept|kept_all|kept_rows|list)\s*(?:::<[^>]*>)?\(\s*&?(?:shelf\(\s*)?([A-Za-z_][\w:]*|"[^"]+")"#,
        )
        .unwrap();
        let mut raw_reads = Vec::new();
        for (file, text) in &found {
            // What the test of the log reads of the store, to see what was kept, is no desk's.
            if file.ends_with("unkept.rs") {
                continue;
            }
            let local: BTreeMap<&str, &str> = constant
                .captures_iter(text)
                .map(|c| (c.get(1).unwrap().as_str(), c.get(2).unwrap().as_str()))
                .collect();
            // Where a desk declares a kind judged when read, its reads go through the readers
            // that judge them (`kept_book`, `kept_listed`): every raw read there names a kind
            // judged at the start.
            let declares = text.contains(".when_read()");
            for c in raw.captures_iter(text) {
                let given = c.get(2).unwrap().as_str();
                let name = match given.strip_prefix('"') {
                    Some(literal) => literal.strip_suffix('"'),
                    None => local.get(given.rsplit("::").next().unwrap()).copied(),
                };
                let line = text[..c.get(0).unwrap().start()].matches('\n').count() + 1;
                let judged_when_read = name.is_some_and(|name| when_read.contains(name));
                let unknown_here = declares && !name.is_some_and(|name| at_start.contains(name));
                if judged_when_read || unknown_here {
                    raw_reads.push(format!("{file}:{line}: {}", c.get(0).unwrap().as_str()));
                }
            }
        }
        assert!(
            raw_reads.is_empty(),
            "a kind judged when read, read raw: {raw_reads:#?}"
        );
    }

    #[test]
    fn each_kind_is_declared_once() {
        let mut seen = BTreeSet::new();
        for kind in super::kinds() {
            assert!(
                seen.insert(kind.table_name()),
                "declared twice: {}",
                kind.name
            );
        }
    }
}
