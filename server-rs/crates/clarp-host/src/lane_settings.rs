//! The owner's settings of lanes from his apps (issue 779): `GET /lane-settings` says each tool's
//! switch, how the limits all lanes share are set (auto from the free memory, or fixed; issue
//! 792) and their values, the idle time, and what the lanes and the machine hold now, with the
//! lanes that live and the turns that started their tool anew in the last hour;
//! `POST /lane-settings` changes any of them. They stay where the Host has always read them, the
//! top-level keys of its own `config.toml` ([`crate::lane::Settings`]), so `install-host` carries
//! them and an edit by hand still works. A change is written into that file with every other
//! byte of it kept, and takes effect at each tool's next turn, as an edit by hand does.

use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use serde_json::{Map, Value, json};

use crate::cold_starts::Counted;
use crate::edge::{ApiError, App};
use crate::lane::{self, Holding, MIB, Memory, Mode, Settings, Tool};

/// The kind of the event every app is told a change by: it reads `GET /lane-settings` again.
pub(crate) const CHANGED: &str = "lane-settings-updated";

/// A limit as the apps name it, the key it is kept under, the values it may be set to, and the
/// value the Host uses now.
struct Limit {
    name: &'static str,
    key: &'static str,
    min: i64,
    max: i64,
    now: fn(&Settings) -> i64,
}

fn most_now(settings: &Settings) -> i64 {
    i64::try_from(settings.most).unwrap_or(i64::MAX)
}

fn memory_now(settings: &Settings) -> i64 {
    i64::try_from(settings.budget / MIB).unwrap_or(i64::MAX)
}

fn idle_now(settings: &Settings) -> i64 {
    i64::try_from(settings.idle_ms / 60_000).unwrap_or(i64::MAX)
}

fn reserve_now(settings: &Settings) -> i64 {
    i64::try_from(settings.reserve_percent).unwrap_or(i64::MAX)
}

fn ceiling_now(settings: &Settings) -> i64 {
    i64::try_from(settings.ceiling).unwrap_or(i64::MAX)
}

/// How the limits are set, as the apps name it, and the values it may be set to.
const MODE: &str = "limits";

/// The limits the owner may set from an app. The file may hold others (an edit by hand); the
/// Host uses what it holds, and an app is refused only a value it changes to.
static LIMITS: [Limit; 5] = [
    Limit {
        name: "most",
        key: lane::MOST,
        min: 1,
        max: 32,
        now: most_now,
    },
    Limit {
        name: "memory_mb",
        key: lane::MEMORY_MB,
        min: 256,
        max: 16_384,
        now: memory_now,
    },
    Limit {
        name: "idle_minutes",
        key: lane::IDLE_MINUTES,
        min: 1,
        max: 240,
        now: idle_now,
    },
    Limit {
        name: "reserve_percent",
        key: lane::RESERVE_PERCENT,
        min: 5,
        max: 50,
        now: reserve_now,
    },
    Limit {
        name: "ceiling",
        key: lane::CEILING,
        min: 1,
        max: 64,
        now: ceiling_now,
    },
];

/// A value a request names, of the right kind; whether it is a change, and within its range,
/// is known only against the file as it is when the change is written.
#[derive(Debug)]
enum Wanted {
    Switch(Tool, bool),
    Limit(&'static Limit, i64),
    Mode(Mode),
}

/// A change to write: the keys of the file and their new values, in the order they are named.
type Change = Vec<(&'static str, toml_edit::Value)>;

/// A refusal an app can tell apart by its `code`, with the setting it is about.
fn refused(status: StatusCode, code: &str, words: String, more: Value) -> ApiError {
    let mut body = json!({"ok": false, "error": words, "code": code});
    if let (Some(body), Value::Object(more)) = (body.as_object_mut(), more) {
        body.extend(more);
    }
    ApiError::custom(status, words, body)
}

fn range(limit: &Limit) -> Value {
    json!({"min": limit.min, "max": limit.max})
}

/// The settings as the Host uses them now, and what the lanes and the machine hold.
fn told(settings: &Settings, holding: &Holding, memory: Option<Memory>, cold: &Counted) -> Value {
    let mb = |bytes: u64| bytes.saturating_add(MIB / 2) / MIB;
    let mut answer = Map::new();
    answer.insert(MODE.to_owned(), json!(settings.mode.name()));
    let mut ranges = Map::new();
    for limit in &LIMITS {
        answer.insert(limit.name.to_owned(), json!((limit.now)(settings)));
        ranges.insert(limit.name.to_owned(), range(limit));
    }
    answer.insert("ranges".to_owned(), Value::Object(ranges));
    let mut tools = Map::new();
    let (mut lanes, mut bytes) = (0, 0u64);
    for (tool, held) in Tool::ALL.iter().zip(&holding.tools) {
        answer.insert(tool.switch().to_owned(), json!(settings.chat(*tool)));
        tools.insert(
            tool.key().to_owned(),
            json!({"lanes": held.lanes, "busy": held.busy, "memory_mb": mb(held.bytes)}),
        );
        lanes += held.lanes;
        bytes = bytes.saturating_add(held.bytes);
    }
    // What the machine has free, what auto keeps free of new lanes, and what is left for them.
    let free = memory.map(|memory| {
        let reserve = settings.reserve_of(memory);
        (
            mb(memory.available),
            mb(reserve),
            mb(memory.available.saturating_sub(reserve)),
        )
    });
    answer.insert(
        "held".to_owned(),
        json!({"lanes": lanes, "memory_mb": mb(bytes), "unanswered": holding.unanswered,
               "tools": tools,
               "available_mb": free.map(|free| free.0),
               "reserve_mb": free.map(|free| free.1),
               "room_mb": free.map(|free| free.2)}),
    );
    // The processes that live, apart from the turns that start a tool anew: many live lanes are
    // fine (the owner's word of 25 Nov 2026), what loads the machine is starting tools.
    let mut live = Map::new();
    let (mut lanes, mut busy) = (0, 0);
    for (tool, held) in Tool::ALL.iter().zip(&holding.tools) {
        live.insert(
            tool.key().to_owned(),
            json!({"lanes": held.lanes, "busy": held.busy,
                   "idle": held.lanes.saturating_sub(held.busy)}),
        );
        lanes += held.lanes;
        busy += held.busy;
    }
    answer.insert(
        "live".to_owned(),
        json!({"lanes": lanes, "busy": busy, "idle": lanes - busy.min(lanes), "tools": live}),
    );
    answer.insert(
        "cold_starts_last_hour".to_owned(),
        json!({"total": cold.total, "tools": cold.tools}),
    );
    Value::Object(answer)
}

/// The answer to both routes: the settings read anew from the file, and the lanes asked, all at
/// once and within [`lane::HELD_LOOK`] together.
async fn now(app: &App) -> Result<Value, ApiError> {
    let deadline = tokio::time::Instant::now() + lane::HELD_LOOK;
    let run = app.config.run_dir.clone();
    let cold = app.cold_starts.clone();
    let (settings, lanes, memory, cold) = tokio::task::spawn_blocking(move || {
        (
            Settings::read(),
            lane::lanes_in(&run),
            Memory::read(),
            cold.counted(),
        )
    })
    .await
    .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "lanes unread"))?;
    Ok(told(
        &settings,
        &lane::held(lanes, deadline).await,
        memory,
        &cold,
    ))
}

/// `GET /lane-settings`: the owner's apps, and an agent's own tools, may read it.
pub(crate) async fn get(State(app): State<App>) -> Result<Json<Value>, ApiError> {
    now(&app).await.map(Json)
}

/// `POST /lane-settings`: only the owner changes it. An agent's tools are kept out by their
/// token's scope; a request that names a turn or a peer Host is refused here, as the owner's
/// other settings refuse it.
pub(crate) async fn set(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if crate::coordination::from_an_agent(&headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    let wanted = asked(&body)?;
    let Some(path) = lane::config_path() else {
        return Err(refused(
            StatusCode::CONFLICT,
            "config_unwritable",
            "this Host was started without --config: it keeps no settings; nothing was changed"
                .into(),
            Value::Null,
        ));
    };
    let written = tokio::task::spawn_blocking(move || write(&path, &wanted))
        .await
        .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "lane settings unkept"))?;
    let changed = written.map_err(|unwritten| match unwritten {
        Unwritten::OutOfRange(limit) => refused(
            StatusCode::BAD_REQUEST,
            "out_of_range",
            format!(
                "{} must be between {} and {}",
                limit.name, limit.min, limit.max
            ),
            json!({"setting": limit.name, "range": range(limit)}),
        ),
        Unwritten::NotToml => refused(
            StatusCode::CONFLICT,
            "config_unreadable",
            "the Host's config.toml is not TOML; nothing was changed".into(),
            Value::Null,
        ),
        Unwritten::NotAValue(key) => refused(
            StatusCode::CONFLICT,
            "config_unreadable",
            format!("{key} in the Host's config.toml is not a value; nothing was changed"),
            Value::Null,
        ),
        Unwritten::Io(why) => {
            eprintln!("clarp-host: the lane settings could not be written: {why}");
            refused(
                StatusCode::INTERNAL_SERVER_ERROR,
                "config_unwritable",
                "the Host's config.toml could not be written; nothing was changed".into(),
                Value::Null,
            )
        }
    })?;
    let answer = now(&app).await?;
    if changed {
        let mut event = json!({"type": CHANGED});
        if let (Some(event), Some(settings)) = (event.as_object_mut(), answer.as_object()) {
            for (key, value) in settings {
                // What is read of the machine now is not a setting: an app reads it anew.
                if !matches!(
                    key.as_str(),
                    "held" | "ranges" | "live" | "cold_starts_last_hour"
                ) {
                    event.insert(key.clone(), value.clone());
                }
            }
        }
        app.hub.publish(event, app.clock.now_ms());
    }
    Ok(Json(answer))
}

/// What a request names, each of the right kind. A key it does not name, or names as null, is
/// left as it is; one this route does not know is passed over, so an app may send back what it
/// read.
fn asked(body: &[u8]) -> Result<Vec<Wanted>, ApiError> {
    let Ok(Value::Object(asked)) = serde_json::from_slice::<Value>(body) else {
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "bad_json",
            "the body must be a JSON object".into(),
            Value::Null,
        ));
    };
    let named = |name: &str| asked.get(name).filter(|value| !value.is_null());
    let mut wanted = Vec::new();
    for tool in Tool::ALL {
        let switch = tool.switch();
        match named(switch) {
            None => {}
            Some(Value::Bool(on)) => wanted.push(Wanted::Switch(tool, *on)),
            Some(_) => {
                return Err(refused(
                    StatusCode::BAD_REQUEST,
                    "wrong_type",
                    format!("{switch} must be true or false"),
                    json!({"setting": switch}),
                ));
            }
        }
    }
    match named(MODE) {
        None => {}
        Some(value) => match value.as_str().and_then(Mode::named) {
            Some(mode) => wanted.push(Wanted::Mode(mode)),
            None => {
                return Err(refused(
                    StatusCode::BAD_REQUEST,
                    "wrong_type",
                    format!("{MODE} must be \"auto\" or \"fixed\""),
                    json!({"setting": MODE}),
                ));
            }
        },
    }
    for limit in &LIMITS {
        let Some(value) = named(limit.name) else {
            continue;
        };
        let Some(number) = value.as_i64() else {
            return Err(refused(
                StatusCode::BAD_REQUEST,
                "wrong_type",
                format!("{} must be a whole number", limit.name),
                json!({"setting": limit.name, "range": range(limit)}),
            ));
        };
        wanted.push(Wanted::Limit(limit, number));
    }
    if wanted.is_empty() {
        let names: Vec<&str> = Tool::ALL
            .iter()
            .map(|tool| tool.switch())
            .chain([MODE])
            .chain(LIMITS.iter().map(|limit| limit.name))
            .collect();
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "nothing_to_change",
            format!("name one of {}", names.join(", ")),
            Value::Null,
        ));
    }
    Ok(wanted)
}

/// Why a change was not written. Nothing of the file changed in any of them.
#[derive(Debug)]
enum Unwritten {
    /// A limit would change to a value outside its range.
    OutOfRange(&'static Limit),
    /// The file is there and is no TOML: it is not written over.
    NotToml,
    /// The key is a table or an array of tables in the file, which a value would replace.
    NotAValue(&'static str),
    Io(std::io::Error),
}

impl std::fmt::Debug for Limit {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.name)
    }
}

impl From<std::io::Error> for Unwritten {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

/// What of `wanted` changes the file whose text is `text`: a value equal to the one the Host uses
/// now is no change, is not checked against its range (a value set by hand outside it comes back
/// as it was read) and is not written, so a send-back never pins a default into the file. A
/// limit that changes must be within its range; nothing is changed when one is not.
fn change_of(text: &str, wanted: &[Wanted]) -> Result<Change, Unwritten> {
    let file: toml::Table = text.parse().map_err(|_| Unwritten::NotToml)?;
    let now = Settings::of(Some(&file));
    let mut change = Change::new();
    for wanted in wanted {
        match wanted {
            // Auto has no count, and fixed counts by `most`: the ceiling bounds nothing, and a
            // request that names it is taken as naming nothing.
            Wanted::Limit(limit, _) if limit.key == lane::CEILING => {}
            Wanted::Switch(tool, on) if now.chat(*tool) != *on => {
                change.push((tool.switch(), (*on).into()));
            }
            Wanted::Limit(limit, number) if (limit.now)(&now) != *number => {
                if !(limit.min..=limit.max).contains(number) {
                    return Err(Unwritten::OutOfRange(limit));
                }
                change.push((limit.key, (*number).into()));
            }
            Wanted::Mode(mode) if now.mode != *mode => {
                change.push((lane::LIMITS, mode.name().into()));
            }
            _ => {}
        }
    }
    // The owner setting a count or a budget is his choice of fixed limits: in auto they would do
    // nothing, so an app that sets them, an old one too, switches the Host to fixed with them
    // (the lead's decision of 25 Nov 2026 on issue 792). A request that names `limits` itself
    // says which.
    let sets_a_fixed_limit = change
        .iter()
        .any(|(key, _)| *key == lane::MOST || *key == lane::MEMORY_MB);
    let names_the_mode = wanted.iter().any(|w| matches!(w, Wanted::Mode(_)));
    if sets_a_fixed_limit && !names_the_mode && now.mode != Mode::Fixed {
        change.push((lane::LIMITS, Mode::Fixed.name().into()));
    }
    Ok(change)
}

/// Writes what of `wanted` is a change into the file at `path`, a symbolic link followed to its
/// file, keeping every other byte of it: its other keys, its tables, its comments, and a comment
/// beside a value that changes. A key the file does not have yet is added among its top-level
/// keys, before any table, where the Host reads it. The file is written whole beside itself,
/// with its own permissions (it holds the Host's token), and renamed over the old one, so a
/// reader sees the old file or the new one and never a part. A change at once, by another
/// request or by `install-host`, takes turns by a lock on the folder. Whether the file changed.
fn write(path: &Path, wanted: &[Wanted]) -> Result<bool, Unwritten> {
    let path = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let folder = path
        .parent()
        .map_or_else(|| PathBuf::from("."), Path::to_path_buf);
    std::fs::create_dir_all(&folder)?;
    let lock = std::fs::File::open(&folder)?;
    lock.lock()?;
    let (text, mode) = match std::fs::read_to_string(&path) {
        Ok(text) => {
            let mode = std::fs::metadata(&path)?.permissions().mode() & 0o7777;
            (text, mode)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (String::new(), 0o600),
        Err(error) => return Err(error.into()),
    };
    let change = change_of(&text, wanted)?;
    if change.is_empty() {
        return Ok(false);
    }
    let text = changed(&text, &change)?;
    let name = path
        .file_name()
        .map_or_else(|| "config.toml".into(), |name| name.to_string_lossy());
    let temporary = folder.join(format!(".{name}.{}.lane-settings", std::process::id()));
    let written = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(mode)
            .open(&temporary)?;
        file.set_permissions(std::fs::Permissions::from_mode(mode))?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        std::fs::rename(&temporary, &path)
    })();
    if written.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    written?;
    // Renamed is written: the next reader sees the change. A folder that cannot be synced only
    // leaves the rename less sure to outlive a crash of the machine, and that is said.
    if let Err(why) = std::fs::File::open(&folder).and_then(|folder| folder.sync_all()) {
        eprintln!("clarp-host: the folder of the lane settings could not be synced: {why}");
    }
    Ok(true)
}

/// The text of the file with `change` in it, and nothing else of it changed.
fn changed(text: &str, change: &Change) -> Result<String, Unwritten> {
    let mut document: toml_edit::DocumentMut = text.parse().map_err(|_| Unwritten::NotToml)?;
    let root = document.as_table_mut();
    for (key, value) in change {
        match root.get_mut(key) {
            Some(toml_edit::Item::Value(old)) => {
                // What stood around the old value (its space, a comment after it) stays.
                let decor = old.decor().clone();
                *old = value.clone();
                *old.decor_mut() = decor;
            }
            None | Some(toml_edit::Item::None) => {
                root.insert(key, toml_edit::Item::Value(value.clone()));
            }
            Some(_) => return Err(Unwritten::NotAValue(key)),
        }
    }
    Ok(document.to_string())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn change(pairs: &[(&'static str, toml_edit::Value)]) -> Change {
        pairs.to_vec()
    }

    const FILE: &str = "# Written by install-host.\n\
        codex_chat_lane = true   # on since the first week\n\
        codex_lane_most = 8\n\
        \n\
        [server]\n\
        kind = \"clarp-host\"\n\
        auth_token = \"secret\"\n\
        \n\
        # the agents' own\n\
        [agents]\n\
        codex_reset_max_per_day = 2\n";

    #[test]
    fn a_change_keeps_every_other_byte_and_the_comment_beside_its_value() {
        let text = changed(
            FILE,
            &change(&[
                ("codex_chat_lane", false.into()),
                ("codex_lane_most", 12i64.into()),
            ]),
        )
        .unwrap();
        assert_eq!(
            text,
            FILE.replace("= true   #", "= false   #")
                .replace("codex_lane_most = 8", "codex_lane_most = 12")
        );
    }

    #[test]
    fn a_new_key_is_a_top_level_key_the_host_reads() {
        let text = changed(
            FILE,
            &change(&[
                ("claude_chat_lane", true.into()),
                ("lane_memory_mb", 3000i64.into()),
            ]),
        )
        .unwrap();
        let read: toml::Table = text.parse().unwrap();
        let settings = Settings::of(Some(&read));
        assert!(settings.chat(Tool::Claude) && settings.chat(Tool::Codex));
        assert_eq!(settings.budget, 3000 * MIB);
        assert_eq!(read["server"]["auth_token"].as_str(), Some("secret"));
        assert_eq!(
            read["agents"]["codex_reset_max_per_day"].as_integer(),
            Some(2)
        );
        // Everything that was there is there, in its place.
        let kept: Vec<&str> = text
            .lines()
            .filter(|line| {
                !line.starts_with("claude_chat_lane") && !line.starts_with("lane_memory_mb")
            })
            .collect();
        assert_eq!(kept, FILE.lines().collect::<Vec<_>>());
        assert!(text.find("lane_memory_mb").unwrap() < text.find("[server]").unwrap());
    }

    #[test]
    fn the_muse_switch_is_read_asked_and_written_as_the_other_tools_are() {
        // Issue 762: Muse's lane is switched from the same screen, by the same key rules.
        let wanted = asked(br#"{"muse_chat_lane": true}"#).unwrap();
        let text = changed(FILE, &change_of(FILE, &wanted).unwrap()).unwrap();
        let read: toml::Table = text.parse().unwrap();
        let settings = Settings::of(Some(&read));
        assert!(settings.chat(Tool::Muse) && settings.chat(Tool::Codex));
        assert!(!settings.chat(Tool::Claude));
        assert!(text.find("muse_chat_lane = true").unwrap() < text.find("[server]").unwrap());
        let answer = told(&settings, &Holding::default(), None, &Counted::default());
        assert_eq!(answer["muse_chat_lane"], true);
        assert_eq!(
            answer["held"]["tools"]["muse"],
            json!({"lanes": 0, "busy": 0, "memory_mb": 0})
        );
        let error = asked(br#"{"muse_chat_lane": "on"}"#).unwrap_err();
        assert_eq!(error.status(), StatusCode::BAD_REQUEST);
        assert_eq!(error.message(), "muse_chat_lane must be true or false");
    }

    #[test]
    fn a_file_that_is_no_toml_or_holds_a_table_under_the_key_is_not_written_over() {
        assert!(matches!(
            changed(
                "this is = = not toml",
                &change(&[("codex_lane_most", 3i64.into())])
            ),
            Err(Unwritten::NotToml)
        ));
        assert!(matches!(
            changed(
                "[codex_lane_most]\na = 1\n",
                &change(&[("codex_lane_most", 3i64.into())])
            ),
            Err(Unwritten::NotAValue("codex_lane_most"))
        ));
    }

    #[test]
    fn a_file_is_written_whole_with_its_permissions() {
        let dir = std::env::temp_dir().join(format!("lane-settings-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        std::fs::write(&path, FILE).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(write(&path, &[Wanted::Limit(&LIMITS[0], 4)]).unwrap());
        let text = std::fs::read_to_string(&path).unwrap();
        // The count set is the owner's choice of fixed limits (issue 792): `lane_limits` is added
        // among the top-level keys, and every other byte of the file, comments included, is kept.
        let without: String = text
            .lines()
            .filter(|line| !line.starts_with("lane_limits"))
            .map(|line| format!("{line}\n"))
            .collect();
        assert_eq!(
            without,
            FILE.replace("codex_lane_most = 8", "codex_lane_most = 4")
        );
        assert!(text.find("lane_limits = \"fixed\"").unwrap() < text.find("[server]").unwrap());
        let read: toml::Table = text.parse().unwrap();
        assert_eq!(Settings::of(Some(&read)).mode, Mode::Fixed);
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        // Nothing is left beside it.
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        // A file that is not there is made, private.
        let fresh = dir.join("fresh").join("config.toml");
        assert!(write(&fresh, &[Wanted::Switch(Tool::Claude, true)]).unwrap());
        assert_eq!(
            std::fs::read_to_string(&fresh).unwrap(),
            "claude_chat_lane = true\n"
        );
        let mode = std::fs::metadata(&fresh).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn values_of_the_wrong_kind_are_refused() {
        for (body, words) in [
            (
                json!({"idle_minutes": 1.5}),
                "idle_minutes must be a whole number",
            ),
            (json!({"most": "8"}), "most must be a whole number"),
            (
                json!({"claude_chat_lane": "yes"}),
                "claude_chat_lane must be true or false",
            ),
            (json!([1]), "the body must be a JSON object"),
        ] {
            let error = asked(body.to_string().as_bytes()).unwrap_err();
            assert_eq!(error.status(), StatusCode::BAD_REQUEST, "{body}");
            assert_eq!(error.message(), words, "{body}");
        }
        let error = asked(br#"{"held": {}}"#).unwrap_err();
        assert!(
            error
                .message()
                .starts_with("name one of codex_chat_lane, claude_chat_lane")
        );
    }

    /// The keys a request changes, against the file `text`.
    fn keys_changed(text: &str, body: Value) -> Result<Vec<&'static str>, Unwritten> {
        let wanted = asked(body.to_string().as_bytes()).unwrap();
        Ok(change_of(text, &wanted)?
            .into_iter()
            .map(|(key, _)| key)
            .collect())
    }

    #[test]
    fn a_value_that_changes_must_be_within_its_range() {
        for (body, name) in [
            (json!({"most": 0}), "most"),
            (json!({"most": 33}), "most"),
            (json!({"memory_mb": 255}), "memory_mb"),
            (json!({"memory_mb": 16_385}), "memory_mb"),
            (json!({"idle_minutes": 241}), "idle_minutes"),
            // One value out of range keeps the right one beside it from being written too.
            (
                json!({"claude_chat_lane": true, "idle_minutes": 0}),
                "idle_minutes",
            ),
        ] {
            match keys_changed(FILE, body.clone()) {
                Err(Unwritten::OutOfRange(limit)) => assert_eq!(limit.name, name, "{body}"),
                other => panic!("{body}: {other:?}"),
            }
        }
        assert_eq!(
            keys_changed(
                FILE,
                json!({"most": 32, "memory_mb": 256, "idle_minutes": 240})
            )
            .unwrap(),
            [
                "codex_lane_most",
                "lane_memory_mb",
                "codex_lane_idle_minutes",
                "lane_limits"
            ]
        );
    }

    #[test]
    fn what_an_app_read_can_be_sent_back_and_only_what_changed_is_written() {
        // Set by hand outside the range an app may set: read as it is used, and sent back with
        // one switch changed, only the switch is written.
        let by_hand = "codex_lane_most = 64\n[server]\nkind = \"clarp-host\"\n";
        let read = told(
            &Settings::of(Some(&by_hand.parse().unwrap())),
            &Holding::default(),
            None,
            &Counted::default(),
        );
        assert_eq!(read["most"], 64);
        let mut sent = read.clone();
        sent["claude_chat_lane"] = json!(true);
        assert_eq!(keys_changed(by_hand, sent).unwrap(), ["claude_chat_lane"]);
        // Sent back as it was read, nothing changes, and no default is pinned into the file.
        assert!(keys_changed(by_hand, read).unwrap().is_empty());
        assert!(
            keys_changed("", json!({"memory_mb": 2200, "most": 8}))
                .unwrap()
                .is_empty()
        );
        let dir = std::env::temp_dir().join(format!("lane-sent-back-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        std::fs::write(&path, by_hand).unwrap();
        let wanted = asked(br#"{"most": 64, "idle_minutes": 15}"#).unwrap();
        assert!(!write(&path, &wanted).unwrap(), "nothing was written");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), by_hand);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_answer_names_each_tool_and_counts_what_the_lanes_hold() {
        let read: toml::Table = "claude_chat_lane = true\ncodex_lane_most = 5\n"
            .parse()
            .unwrap();
        let holding = Holding {
            tools: [
                lane::Held {
                    lanes: 2,
                    busy: 1,
                    bytes: 300 * MIB,
                },
                lane::Held::default(),
                lane::Held::default(),
                lane::Held::default(),
            ],
            unanswered: 1,
        };
        // A machine of 10,000 MiB with 3,000 free: auto keeps 2,000 (20%) free of new lanes.
        let memory = Memory {
            available: 3_000 * MIB,
            total: 10_000 * MIB,
        };
        let answer = told(
            &Settings::of(Some(&read)),
            &holding,
            Some(memory),
            &Counted::default(),
        );
        assert_eq!(
            answer,
            json!({
                "codex_chat_lane": false,
                "claude_chat_lane": true,
                "muse_chat_lane": false,
                "opencode_chat_lane": false,
                "limits": "auto",
                "most": 5,
                "memory_mb": 2200,
                "idle_minutes": 15,
                "reserve_percent": 20,
                "ceiling": 24,
                "ranges": {
                    "most": {"min": 1, "max": 32},
                    "memory_mb": {"min": 256, "max": 16384},
                    "idle_minutes": {"min": 1, "max": 240},
                    "reserve_percent": {"min": 5, "max": 50},
                    "ceiling": {"min": 1, "max": 64},
                },
                "held": {
                    "lanes": 2,
                    "memory_mb": 300,
                    "unanswered": 1,
                    "tools": {
                        "codex": {"lanes": 2, "busy": 1, "memory_mb": 300},
                        "claude": {"lanes": 0, "busy": 0, "memory_mb": 0},
                        "muse": {"lanes": 0, "busy": 0, "memory_mb": 0},
                        "opencode": {"lanes": 0, "busy": 0, "memory_mb": 0},
                    },
                    "available_mb": 3000,
                    "reserve_mb": 2000,
                    "room_mb": 1000,
                },
                "live": {
                    "lanes": 2,
                    "busy": 1,
                    "idle": 1,
                    "tools": {
                        "codex": {"lanes": 2, "busy": 1, "idle": 1},
                        "claude": {"lanes": 0, "busy": 0, "idle": 0},
                        "muse": {"lanes": 0, "busy": 0, "idle": 0},
                        "opencode": {"lanes": 0, "busy": 0, "idle": 0},
                    },
                },
                "cold_starts_last_hour": {"total": 0, "tools": {}},
            })
        );
        // Where the machine's memory cannot be read, the Host says so by nothing.
        let answer = told(
            &Settings::of(Some(&read)),
            &holding,
            None,
            &Counted::default(),
        );
        assert_eq!(answer["held"]["available_mb"], Value::Null);
        assert_eq!(answer["held"]["room_mb"], Value::Null);
    }

    #[test]
    fn the_limits_are_set_auto_or_fixed_by_name_and_only_a_change_is_written() {
        assert_eq!(
            keys_changed(FILE, json!({"limits": "fixed"})).unwrap(),
            ["lane_limits"]
        );
        assert!(
            keys_changed(FILE, json!({"limits": "auto"}))
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            keys_changed(FILE, json!({"reserve_percent": 25, "ceiling": 16})).unwrap(),
            ["lane_reserve_percent"]
        );
        // The ceiling bounds nothing: a request that sets it, in range or not, changes nothing.
        assert!(
            keys_changed(FILE, json!({"ceiling": 65}))
                .unwrap()
                .is_empty()
        );
        assert!(
            keys_changed(FILE, json!({"ceiling": 3}))
                .unwrap()
                .is_empty()
        );
        for (body, name) in [
            (json!({"reserve_percent": 4}), "reserve_percent"),
            (json!({"reserve_percent": 51}), "reserve_percent"),
        ] {
            match keys_changed(FILE, body.clone()) {
                Err(Unwritten::OutOfRange(limit)) => assert_eq!(limit.name, name, "{body}"),
                other => panic!("{body}: {other:?}"),
            }
        }
        for body in [json!({"limits": "dynamic"}), json!({"limits": 1})] {
            let error = asked(body.to_string().as_bytes()).unwrap_err();
            assert_eq!(error.status(), StatusCode::BAD_REQUEST, "{body}");
            assert_eq!(error.message(), "limits must be \"auto\" or \"fixed\"");
        }
        // A count or a budget set from any app, an old one too, is the owner's choice of fixed
        // limits; naming the mode as well says which, and a value that does not change is no choice.
        assert_eq!(
            keys_changed(FILE, json!({"most": 4})).unwrap(),
            ["codex_lane_most", "lane_limits"]
        );
        assert_eq!(
            keys_changed(FILE, json!({"memory_mb": 3000})).unwrap(),
            ["lane_memory_mb", "lane_limits"]
        );
        assert_eq!(
            keys_changed(FILE, json!({"most": 4, "limits": "auto"})).unwrap(),
            ["codex_lane_most"]
        );
        assert!(keys_changed(FILE, json!({"most": 8})).unwrap().is_empty());
        let fixed = "lane_limits = \"fixed\"\ncodex_lane_most = 8\n";
        assert_eq!(
            keys_changed(fixed, json!({"most": 4})).unwrap(),
            ["codex_lane_most"]
        );
        let text = changed(FILE, &change(&[("lane_limits", "fixed".into())])).unwrap();
        let read: toml::Table = text.parse().unwrap();
        assert_eq!(Settings::of(Some(&read)).mode, Mode::Fixed);
    }

    #[test]
    fn the_answer_counts_the_cold_starts_of_the_last_hour_and_says_nothing_of_pressure() {
        let cold = Counted {
            total: 3,
            tools: [("claude".to_owned(), 2), ("scripted".to_owned(), 1)].into(),
        };
        let answer = told(&Settings::of(None), &Holding::default(), None, &cold);
        assert_eq!(
            answer["cold_starts_last_hour"],
            json!({"total": 3, "tools": {"claude": 2, "scripted": 1}})
        );
        // No turn waits for the machine's pressure (the owner, 25 Nov 2026): nothing of it is told.
        for gone in ["pressure", "held_for_pressure", "pressure_io_percent"] {
            assert!(answer.get(gone).is_none(), "{gone}: {answer}");
        }
    }

    #[test]
    fn the_old_pressure_thresholds_are_no_settings() {
        // An app built when the Host held turns for the machine's pressure may send its
        // thresholds: alone they name no setting, as any unknown key, and beside a setting they
        // are passed over and nothing of them is written.
        let alone = asked(
            br#"{"pressure_io_percent": 40, "pressure_cpu_percent": 60,
                 "pressure_memory_percent": 5}"#,
        )
        .unwrap_err();
        assert_eq!(alone.status(), StatusCode::BAD_REQUEST);
        assert!(alone.message().starts_with("name one of codex_chat_lane"));
        assert_eq!(
            keys_changed(
                FILE,
                json!({"pressure_io_percent": 40, "claude_chat_lane": true})
            )
            .unwrap(),
            ["claude_chat_lane"]
        );
    }
}
