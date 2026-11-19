//! Read-only workspace suggestions for the new-agent dialog.
use crate::edge::App;
use axum::{
    Json, Router,
    extract::{Query, State},
    routing::get,
};
use clarp_kernel::{launch_directories as rules, python};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

async fn zoxide(args: &[&str]) -> Option<(bool, String)> {
    let program = std::env::var_os("CLARP_ZOXIDE_PROGRAM").unwrap_or_else(|| "zoxide".into());
    let mut cmd = tokio::process::Command::new(program);
    cmd.args(args)
        .kill_on_drop(true)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for name in crate::OWN_SECRETS {
        cmd.env_remove(name);
    }
    for (name, value) in crate::child_environment() {
        if let Some(value) = value {
            cmd.env(name, value);
        } else {
            cmd.env_remove(name);
        }
    }
    let child = cmd.spawn().ok()?;
    let output = tokio::time::timeout(Duration::from_millis(500), child.wait_with_output())
        .await
        .ok()?
        .ok()?;
    Some((
        output.status.success(),
        String::from_utf8(output.stdout).ok()?,
    ))
}

async fn ranked(query: &str) -> Vec<String> {
    let args: Vec<_> = ["query", "--list", "--"]
        .into_iter()
        .chain(query.split(python::blank).filter(|w| !w.is_empty()))
        .collect();
    match zoxide(&args).await {
        Some((true, stdout)) => rules::ranked(&stdout, query, false),
        Some((false, _)) if !query.is_empty() => zoxide(&["query", "--list"])
            .await
            .map_or_else(Vec::new, |(_, stdout)| rules::ranked(&stdout, query, true)),
        _ => Vec::new(),
    }
}

fn explicit(q: &str, home: &str) -> Vec<String> {
    if q.is_empty() {
        return Vec::new();
    }
    let path = PathBuf::from(rules::explicit_path(q, home));
    let mut found = Vec::new();
    if path.is_dir() {
        found.push(path.to_string_lossy().into_owned());
    }
    let (parent, prefix) = if q.ends_with('/') {
        (&*path, "")
    } else {
        (
            path.parent().unwrap_or(&path),
            path.file_name()
                .and_then(|s| s.to_str())
                .unwrap_or(if path.ends_with("..") { ".." } else { "" }),
        )
    };
    if let Ok(entries) = std::fs::read_dir(parent) {
        let names = entries
            .flatten()
            .filter(|e| e.path().is_dir())
            .filter_map(|e| e.file_name().into_string().ok())
            .collect();
        found.extend(
            rules::completions(names, prefix)
                .into_iter()
                .map(|n| parent.join(n).to_string_lossy().into_owned()),
        );
    }
    found
}

fn expand(path: &str, home: &str) -> Option<String> {
    let path = rules::spelling(path);
    let Some(rest) = path.strip_prefix('~') else {
        return Some(path);
    };
    let (user, suffix) = rest.split_once('/').unwrap_or((rest, ""));
    let base = if user.is_empty() {
        PathBuf::from(home)
    } else {
        nix::unistd::User::from_name(user).ok()??.dir
    };
    Some(rules::spelling(&base.join(suffix).to_string_lossy()))
}

type Params = Query<Vec<(String, String)>>;
fn first<'a>(params: &'a [(String, String)], key: &str) -> &'a str {
    params
        .iter()
        .find(|(k, v)| k == key && !v.is_empty())
        .map_or("", |(_, v)| v)
}

async fn lookup(State(app): State<App>, Query(params): Params) -> Json<Value> {
    let q = rules::query(first(&params, "q")).to_owned();
    let ranked = ranked(&q).await;
    let directory = app.fleet.directory();
    let home = rules::spelling(&crate::config::home().to_string_lossy());
    let result = tokio::task::spawn_blocking(move || {
        let recent = rules::recent(&directory.launch_usage, &directory.cwd_owners);
        let explicit = explicit(&q, &home);
        rules::lookup(
            &home,
            &q,
            recent,
            ranked,
            explicit,
            |p| expand(p, &home),
            |p| Path::new(p).is_dir(),
        )
    })
    .await
    .unwrap_or_default();
    Json(result)
}

async fn favorites(State(app): State<App>, Query(params): Params) -> Json<Value> {
    let held = app.fleet.directory();
    Json(rules::favorites(
        &held.launch_usage,
        first(&params, "limit"),
    ))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/launch-directories", get(lookup))
        .route(
            "/favorite-paths",
            get(favorites).post(crate::edge::no_route),
        )
}
