//! The vocabulary generated for one turn, in the order of the reference generators.
use super::{Pack, Term, WORD, rarity};
use crate::python;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Sources {
    pub agent_names: Vec<String>,
    pub project_name: String,
    pub identifiers: Vec<String>,
    pub branches: Vec<String>,
    pub commit_subjects: Vec<String>,
    pub recent_transcripts: Vec<String>,
    pub corrections: Vec<(String, String)>,
}

pub fn term(text: &str, pack: &str) -> Term {
    Term {
        text: text.into(),
        pack: pack.into(),
        rarity: rarity(text),
        recency: 0.0,
        confusable: false,
        say_as: None,
    }
}

fn salient(s: &str) -> impl Iterator<Item = &str> {
    WORD.find_iter(s)
        .map(|m| m.as_str())
        .filter(|w| w.len() >= 3 && rarity(w) >= 0.8)
}

fn unique(words: Vec<Term>) -> Vec<Term> {
    let mut seen = HashSet::new();
    words
        .into_iter()
        .filter(|t| seen.insert(python::casefold(&t.text)))
        .collect()
}

impl Sources {
    pub fn packs(&self) -> Vec<Pack> {
        let mut packs = Vec::new();
        let mut append = |name: &str, priority, floor, terms: Vec<Term>| {
            if !terms.is_empty() {
                packs.push(Pack {
                    name: name.into(),
                    terms,
                    priority,
                    floor,
                    enabled: true,
                });
            }
        };
        let corrections = self
            .corrections
            .iter()
            .filter_map(|(heard, intended)| {
                let value = python::strip(intended);
                if value.is_empty() {
                    return None;
                }
                let mut t = term(value, "corrections");
                t.rarity = t.rarity.max(0.9);
                t.confusable = true;
                t.say_as = Some(python::strip(heard))
                    .filter(|s| !s.is_empty())
                    .map(str::to_string);
                Some(t)
            })
            .collect();
        append("corrections", 3.0, 2, unique(corrections));
        let mut recent = Vec::new();
        for (i, s) in self.recent_transcripts.iter().enumerate() {
            for word in salient(s) {
                let mut t = term(word, "recent-speech");
                t.recency = 1.0 - i as f64 / self.recent_transcripts.len() as f64;
                recent.push(t);
            }
        }
        let mut recent = unique(recent);
        recent.sort_by(|a, b| {
            b.recency
                .total_cmp(&a.recency)
                .then_with(|| b.rarity.total_cmp(&a.rarity))
        });
        recent.truncate(24);
        append("recent-speech", 2.5, 0, recent);
        let names = self
            .agent_names
            .iter()
            .map(|s| python::strip(s))
            .filter(|s| !s.is_empty())
            .map(|s| {
                let mut t = term(s, "agents");
                t.rarity = 0.9;
                t
            })
            .collect();
        append("agents", 2.0, 2, unique(names));
        let mut workspace = Vec::new();
        let project = python::strip(&self.project_name);
        if !project.is_empty() {
            let mut t = term(project, "workspace");
            t.rarity = t.rarity.max(0.95);
            workspace.push(t);
        }
        for ident in &self.identifiers {
            let id = python::strip(ident);
            if !id.is_empty() {
                workspace.push(term(id, "workspace"));
            }
        }
        for branch in &self.branches {
            let parts: Vec<_> = python::strip(branch)
                .split(['/', '_', '-'])
                .filter(|s| {
                    !s.is_empty() && !python::isdigit(s) && !s.eq_ignore_ascii_case("worktree")
                })
                .take(6)
                .collect();
            if !parts.is_empty() {
                workspace.push(term(&parts.join(" "), "workspace"));
            }
        }
        for s in &self.commit_subjects {
            workspace.extend(salient(s).map(|w| term(w, "workspace")));
        }
        append("workspace", 1.2, 0, unique(workspace));
        packs
    }
}

pub const SKIP: &[&str] = &[
    "node_modules/",
    ".git/",
    "dist/",
    "build/",
    "vendor/",
    ".venv/",
    "linuxbrew/",
    ".linuxbrew/",
    "site-packages/",
    "dist-packages/",
    ".cache/",
    ".local/share/",
    ".rustup/",
    ".cargo/registry/",
    "toolchains/",
    "DerivedData/",
    ".build/",
    "target/debug/",
    "target/release/",
];
const NOISE: &str = "src lib test tests spec index main utils util common core base types config readme license docs static public assets dist build node_modules vendor init setup package requirements makefile dockerfile";

pub fn identifiers(paths: &[String]) -> Vec<String> {
    let mut counts = BTreeMap::new();
    for path in paths.iter().take(3000) {
        if SKIP.iter().any(|s| path.contains(s)) {
            continue;
        }
        let stem = std::path::Path::new(path)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        let mut take = |t: &str| {
            let low = t.to_ascii_lowercase();
            if t.len() < 4
                || NOISE.split_whitespace().any(|s| s == low)
                || t.bytes().all(|b| b.is_ascii_digit())
                || (low.starts_with("lib") && low.len() >= 5)
                || t.as_bytes()
                    .windows(2)
                    .any(|p| p[0].is_ascii_alphabetic() && p[1].is_ascii_digit())
            {
                return;
            }
            counts.entry(low).or_insert_with(|| (0, t.to_string())).0 += 1;
        };
        for chunk in stem
            .split(|c: char| !c.is_ascii_alphanumeric())
            .filter(|s| !s.is_empty())
        {
            take(chunk);
            let mut start = 0;
            for (i, p) in chunk.as_bytes().windows(2).enumerate() {
                if (p[0].is_ascii_lowercase() || p[0].is_ascii_digit()) && p[1].is_ascii_uppercase()
                {
                    take(&chunk[start..i + 1]);
                    start = i + 1;
                }
            }
            if start > 0 {
                take(&chunk[start..]);
            }
        }
    }
    let mut ranked: Vec<_> = counts.into_values().collect();
    ranked.sort_by_key(|r| std::cmp::Reverse(r.0));
    ranked.into_iter().take(400).map(|(_, s)| s).collect()
}

#[cfg(test)]
mod tests {
    use super::Sources;
    use crate::vocabulary::rank;
    use serde_json::{Value, json};

    #[test]
    #[allow(clippy::unwrap_used)]
    fn ranked_personas_have_the_reference_score_regardless_of_spelling() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/vocabulary-packs-vectors.json"
        ))
        .unwrap();
        let story = &fixture["ranked_names"];
        let sources = Sources {
            agent_names: serde_json::from_value(story["route"].clone()).unwrap(),
            ..Sources::default()
        };
        let packs = sources.packs();
        let pack = packs.iter().find(|pack| pack.name == "agents").unwrap();
        let scores: Vec<_> = pack.terms.iter().map(|term| rank(term, pack)).collect();
        assert_eq!(json!(scores), story["scores"]);
    }
}
