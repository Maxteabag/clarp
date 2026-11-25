//! The public PWA shell and its assets from the Host's configured static tree.

use std::fs::File;
use std::io::{self, Read, Seek};
use std::path::Path as FilePath;
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use axum::Router;
use axum::body::Body;
use axum::extract::{Path, RawQuery, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use clarp_kernel::media::{self, Part};
use regex::Regex;
use sha2::{Digest, Sha256};

use clarp_ports::blobs::Blobs;

use crate::edge::App;

const REVALIDATE: &str = "no-cache";
const IMMUTABLE: &str = "public, max-age=31536000, immutable";

pub(crate) fn public_root(path: &str) -> bool {
    matches!(
        path,
        "/" | "/sw.js" | "/manifest.json" | "/styles.css" | "/icon.png"
    )
}

pub(crate) fn routes() -> Router<App> {
    Router::new()
        .route("/", get(root_file))
        .route("/manifest.json", get(root_file))
        .route("/styles.css", get(root_file))
        .route("/icon.png", get(root_file))
        .route("/sw.js", get(root_file))
        .route("/viz", get(root_file))
        .route("/static/", get(|| async { not_found() }))
        .route("/static/{*path}", get(asset))
}

async fn root_file(
    State(app): State<App>,
    uri: Uri,
    RawQuery(query): RawQuery,
    headers: HeaderMap,
) -> Response {
    let path = uri.path().trim_start_matches('/').to_owned();
    serve(app, path, query, headers, true).await
}

async fn asset(
    State(app): State<App>,
    Path(path): Path<String>,
    RawQuery(query): RawQuery,
    headers: HeaderMap,
) -> Response {
    serve(app, path, query, headers, false).await
}

async fn serve(
    app: App,
    path: String,
    query: Option<String>,
    headers: HeaderMap,
    root_file: bool,
) -> Response {
    let Some(root) = app.config.static_dir.clone() else {
        return not_found();
    };
    let blobs = app.blobs.clone();
    tokio::task::spawn_blocking(move || {
        let Ok(root) = root.canonicalize() else {
            return not_found();
        };
        let read = RequestHeaders::new(query.as_deref(), &headers);
        match (root_file, path.as_str()) {
            (true, "") => read.index(&root),
            (true, "sw.js") => read.service_worker(&root),
            (true, "viz") => read.file(&root, "viz.html", false),
            (false, _) => read
                .thumbnail(&root, &path, blobs.as_ref())
                .unwrap_or_else(|| read.file(&root, &path, true)),
            _ => read.file(&root, &path, true),
        }
    })
    .await
    .unwrap_or_else(|_| not_found())
}

fn plain(status: StatusCode, text: &'static str) -> Response {
    (status, [(header::CONTENT_TYPE, "text/plain")], text).into_response()
}

fn not_found() -> Response {
    plain(StatusCode::NOT_FOUND, "not found")
}

/// Allow links within the static tree. Open the resolved relative path without
/// following links again, so substituting a link between resolution and open
/// cannot serve a file from outside the tree.
fn open_asset(root: &FilePath, relative: &str) -> Result<File, StatusCode> {
    if relative.contains('\0')
        || relative
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(StatusCode::FORBIDDEN);
    }
    let resolved = root
        .join(relative)
        .canonicalize()
        .map_err(|_| StatusCode::NOT_FOUND)?;
    let named = resolved
        .strip_prefix(root)
        .map_err(|_| StatusCode::FORBIDDEN)?;
    let named = named.to_str().ok_or(StatusCode::NOT_FOUND)?;
    let (_, file) = crate::files::open_beneath(root, named, false).map_err(|why| why.status())?;
    if !file.metadata().is_ok_and(|found| found.is_file()) {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(file)
}

fn asset_error(status: StatusCode) -> Response {
    if status == StatusCode::FORBIDDEN {
        plain(status, "forbidden")
    } else {
        not_found()
    }
}

fn kind(path: &str) -> &'static str {
    match FilePath::new(path)
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "html" | "htm" => "text/html",
        "js" | "mjs" => "application/javascript",
        "css" => "text/css",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/vnd.microsoft.icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "webmanifest" => "application/manifest+json",
        "txt" => "text/plain",
        "md" => "text/markdown",
        _ => "application/octet-stream",
    }
}

fn version(hash: &[u8]) -> String {
    hash.iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn file_version(mut file: &File) -> io::Result<String> {
    let mut hash = Sha256::new();
    io::copy(&mut file, &mut hash)?;
    Ok(version(&hash.finalize()))
}

fn newest_mtime(root: &FilePath) -> u64 {
    let Ok(root) = crate::files::open(root, true) else {
        return 0;
    };
    let mut todo = vec![root];
    let mut newest = 0;
    while let Some(directory) = todo.pop() {
        let Ok(entries) = std::fs::read_dir(crate::files::beneath(&directory, "")) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(found) = entry.metadata() else {
                continue;
            };
            if found.is_dir() {
                if let Ok(child) = crate::files::open(&entry.path(), true) {
                    todo.push(child);
                }
            } else if found.is_file()
                && let Ok(modified) = found.modified()
                && let Ok(since_epoch) = modified.duration_since(UNIX_EPOCH)
            {
                newest = newest.max(since_epoch.as_secs());
            }
        }
    }
    newest
}

struct RequestHeaders<'a> {
    range: &'a str,
    if_none_match: &'a str,
    version: String,
    /// The size of thumbnail asked for (`px`, feature `avatar_thumbnails`), when it is one.
    px: Option<u32>,
    gzip: bool,
}

impl<'a> RequestHeaders<'a> {
    fn new(query: Option<&str>, headers: &'a HeaderMap) -> Self {
        let text = |name| {
            headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .unwrap_or("")
        };
        let said = |name: &str| {
            url::form_urlencoded::parse(query.unwrap_or_default().as_bytes())
                .find(|(key, value)| key == name && !value.is_empty())
                .map_or_else(String::new, |(_, value)| value.into_owned())
        };
        let version = said("v");
        Self {
            range: text(header::RANGE),
            if_none_match: text(header::IF_NONE_MATCH),
            version,
            px: crate::thumbnails::size(Some(&said("px"))),
            gzip: text(header::ACCEPT_ENCODING)
                .to_ascii_lowercase()
                .contains("gzip"),
        }
    }

    fn matches(&self, tag: &str) -> bool {
        self.if_none_match
            .split(',')
            .map(str::trim)
            .any(|candidate| {
                candidate == "*" || candidate.strip_prefix("W/").unwrap_or(candidate) == tag
            })
    }

    fn compresses(&self, mime: &str) -> bool {
        self.gzip
            && (mime.starts_with("text/")
                || matches!(mime, "application/json" | "application/javascript"))
    }

    fn file(&self, root: &FilePath, path: &str, cached: bool) -> Response {
        let mut file = match open_asset(root, path) {
            Ok(file) => file,
            Err(status) => return asset_error(status),
        };
        let policy = if cached {
            let Ok(version) = file_version(&file) else {
                return not_found();
            };
            let cache = if self.version == version {
                IMMUTABLE
            } else {
                REVALIDATE
            };
            let tag = format!("\"{version}\"");
            if cache == REVALIDATE && self.matches(&tag) {
                return with_cache(StatusCode::NOT_MODIFIED.into_response(), cache, &tag);
            }
            Some((cache, tag))
        } else {
            None
        };
        let mime = kind(path);
        let response = if self.compresses(mime) {
            let mut bytes = Vec::new();
            if file
                .rewind()
                .and_then(|()| file.read_to_end(&mut bytes))
                .is_err()
            {
                return not_found();
            }
            self.ranged_bytes(bytes, mime)
        } else {
            crate::voice::send_open_file(file, mime, self.range)
        };
        match policy {
            Some((cache, tag)) if response.status().is_success() => {
                with_cache(response, cache, &tag)
            }
            _ => response,
        }
    }

    /// A square JPEG of a picture this Host ships for agents' faces, when `px` asks for one:
    /// made once and kept by what is in the file, and kept by whoever fetched it as the
    /// file itself is. `None`: none was asked for, the file is not such a picture, or it
    /// cannot be read as one; the file is then sent whole.
    fn thumbnail(&self, root: &FilePath, path: &str, blobs: &dyn Blobs) -> Option<Response> {
        let px = self.px?;
        if !(path.starts_with("avatars/") || path.starts_with("janitor-avatars/")) {
            return None;
        }
        let mut file = open_asset(root, path).ok()?;
        let version = file_version(&file).ok()?;
        let kept_as = crate::thumbnails::shipped_name(&version, px);
        let kept = crate::thumbnails::kept_or_made(blobs, &kept_as, px, || {
            if file.metadata().ok()?.len() > crate::thumbnails::SOURCE_MOST {
                return None;
            }
            let mut bytes = Vec::new();
            file.rewind().ok()?;
            file.read_to_end(&mut bytes).ok()?;
            Some(bytes)
        })?;
        let bytes = crate::thumbnails::read_whole(blobs, &kept)?;
        let cache = if self.version == version {
            IMMUTABLE
        } else {
            REVALIDATE
        };
        let tag = format!("\"{version}-px{px}\"");
        if cache == REVALIDATE && self.matches(&tag) {
            let unchanged = StatusCode::NOT_MODIFIED.into_response();
            return Some(with_cache(unchanged, cache, &tag));
        }
        let response = self.ranged_bytes(bytes, "image/jpeg");
        Some(if response.status().is_success() {
            with_cache(response, cache, &tag)
        } else {
            response
        })
    }

    fn index(&self, root: &FilePath) -> Response {
        static REFS: LazyLock<Option<Regex>> = LazyLock::new(|| {
            Regex::new(r#"((?:src|href)=["'])(/static/[^"'?#]+|/styles\.css)(["'])"#).ok()
        });
        let html = match read_text(root, "index.html") {
            Ok(html) => html,
            Err(status) => return asset_error(status),
        };
        let html = REFS.as_ref().map_or_else(
            || html.clone(),
            |refs| {
                refs.replace_all(&html, |capture: &regex::Captures<'_>| {
                    let url = &capture[2];
                    let path = url
                        .strip_prefix("/static/")
                        .unwrap_or_else(|| url.trim_start_matches('/'));
                    let Some(version) = open_asset(root, path)
                        .ok()
                        .and_then(|file| file_version(&file).ok())
                    else {
                        return capture[0].to_owned();
                    };
                    format!("{}{url}?v={version}{}", &capture[1], &capture[3])
                })
                .into_owned()
            },
        );
        let tag = format!("\"{}\"", version(&Sha256::digest(&html)));
        let response = if self.matches(&tag) {
            StatusCode::NOT_MODIFIED.into_response()
        } else {
            self.bytes(StatusCode::OK, html.into_bytes(), "text/html")
        };
        with_cache(response, REVALIDATE, &tag)
    }

    fn service_worker(&self, root: &FilePath) -> Response {
        let script = match read_text(root, "sw.js") {
            Ok(script) => script,
            Err(status) => return asset_error(status),
        };
        let changed = format!("'claude-pwa-{}'", newest_mtime(root));
        let script = script.replacen("'claude-pwa-v1'", &changed, 1);
        self.bytes(
            StatusCode::OK,
            script.into_bytes(),
            "application/javascript",
        )
    }

    fn ranged_bytes(&self, mut bytes: Vec<u8>, mime: &'static str) -> Response {
        let total = bytes.len() as u64;
        let (status, mime, content_range) = match media::byte_range(self.range, total) {
            Part::Whole => (StatusCode::OK, mime, None),
            Part::Bytes { first, last } => {
                bytes.truncate(last as usize + 1);
                bytes = bytes.split_off(first as usize);
                (
                    StatusCode::PARTIAL_CONTENT,
                    mime,
                    Some(format!("bytes {first}-{last}/{total}")),
                )
            }
            Part::Missing => {
                bytes.clear();
                (
                    StatusCode::RANGE_NOT_SATISFIABLE,
                    "text/plain",
                    Some(format!("bytes */{total}")),
                )
            }
        };
        let mut response = self.bytes(status, bytes, mime);
        let headers = response.headers_mut();
        headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
        if let Some(value) = content_range.and_then(|range| HeaderValue::from_str(&range).ok()) {
            headers.insert(header::CONTENT_RANGE, value);
        }
        response
    }

    fn bytes(&self, status: StatusCode, bytes: Vec<u8>, mime: &'static str) -> Response {
        let mut encoded = None;
        if bytes.len() as u64 >= crate::packed::SMALLEST && self.compresses(mime) {
            encoded = crate::packed::gzip(&bytes);
        }
        let gzipped = encoded.is_some();
        let mut response = (
            status,
            [(header::CONTENT_TYPE, mime)],
            Body::from(encoded.unwrap_or(bytes)),
        )
            .into_response();
        if gzipped {
            response
                .headers_mut()
                .insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
            response
                .headers_mut()
                .insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
        }
        response
    }
}

fn read_text(root: &FilePath, path: &str) -> Result<String, StatusCode> {
    let mut file = open_asset(root, path)?;
    let mut text = String::new();
    file.read_to_string(&mut text)
        .map_err(|_| StatusCode::NOT_FOUND)?;
    // Python's text reader handles universal newlines.
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

fn with_cache(mut response: Response, cache: &'static str, tag: &str) -> Response {
    let headers = response.headers_mut();
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    if let Ok(tag) = HeaderValue::from_str(tag) {
        headers.insert(header::ETAG, tag);
    }
    response
}
