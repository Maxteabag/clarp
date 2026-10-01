//! What an agent's working directory is: a plain folder, a Git repository or
//! a linked worktree. Port of `desktop/src/app/WorkspaceContext`. Reads only
//! local Git metadata when the Host shares this filesystem; never runs Git
//! and never infers identity from directory names a remote Host reports.

use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::json::Object;

fn first_line(path: &Path) -> String {
    std::fs::read_to_string(path).ok().and_then(|text| text.lines().next().map(|l| l.trim().to_owned())).unwrap_or_default()
}

/// QDir::cleanPath: resolve `.` and `..` lexically.
fn clean(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn name(path: &Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

pub fn inspect(path: &str, shared: bool) -> Object {
    let mut result = json!({"kind": "directory", "path": path, "label": path, "verified": false})
        .as_object()
        .cloned()
        .unwrap_or_default();
    let target = Path::new(path);
    if !shared || !target.is_absolute() || !target.is_dir() {
        return result;
    }
    let mut directory = Some(target);
    while let Some(dir) = directory {
        directory = dir.parent();
        let marker = dir.join(".git");
        if !marker.exists() {
            continue;
        }
        let git_path = if marker.is_dir() {
            marker.clone()
        } else {
            let pointer = first_line(&marker);
            let Some(gitdir) = pointer.strip_prefix("gitdir:") else { return result };
            clean(&dir.join(gitdir.trim()))
        };
        if !git_path.is_dir() {
            return result;
        }
        let common = first_line(&git_path.join("commondir"));
        let worktree = !common.is_empty();
        let common_path = if worktree { clean(&git_path.join(&common)) } else { git_path.clone() };
        if !common_path.is_dir() {
            return result;
        }
        let repository = if !worktree {
            name(dir)
        } else if name(&common_path) == ".git" {
            common_path.parent().map(name).unwrap_or_default()
        } else {
            name(&common_path)
        };
        let relative = target.strip_prefix(dir).map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
        let mut label = repository.clone();
        if worktree {
            label += &format!(" / {}", name(dir));
        }
        if !relative.is_empty() {
            label += &format!(" / {relative}");
        }
        result.insert("kind".into(), json!(if worktree { "worktree" } else { "repo" }));
        result.insert("repository".into(), json!(repository));
        result.insert("worktree".into(), json!(if worktree { name(dir) } else { String::new() }));
        result.insert("root".into(), json!(dir.to_string_lossy()));
        result.insert("label".into(), json!(label));
        result.insert("verified".into(), Value::Bool(true));
        return result;
    }
    result.insert("verified".into(), Value::Bool(true));
    result
}

/// `inspect`, cached for five seconds per path and filesystem scope.
#[derive(Debug, Default)]
pub struct WorkspaceContext {
    cache: HashMap<String, (Instant, Object)>,
}

impl WorkspaceContext {
    pub fn describe(&mut self, path: &str, shared: bool) -> Object {
        let key = format!("{}{path}", if shared { "local:" } else { "host:" });
        if let Some((checked, details)) = self.cache.get(&key)
            && checked.elapsed() < Duration::from_secs(5) {
                return details.clone();
            }
        let details = inspect(path, shared);
        if self.cache.len() >= 128 {
            self.cache.clear();
        }
        self.cache.insert(key, (Instant::now(), details.clone()));
        details
    }
}
