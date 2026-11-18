//! Native OpenCode history, shared by the full catalogue and the DeepSeek card.
use clarp_kernel::{
    failures, python, told,
    tools::{self, cells},
};
use rusqlite::{Connection, OpenFlags, params};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub fn database(home: &Path) -> PathBuf {
    let env = |key| {
        std::env::var_os(key)
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
    };
    env("CLAUDE_PWA_OPENCODE_HOME")
        .unwrap_or_else(|| {
            env("XDG_DATA_HOME")
                .unwrap_or_else(|| home.join(".local/share"))
                .join("opencode")
        })
        .join("opencode.db")
}
fn open(path: &Path) -> Option<Connection> {
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).ok()
}
fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a
            .iter()
            .map(text)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(_) => ["text", "content", "delta"]
            .iter()
            .find_map(|k| v[k].as_str().map(str::to_owned))
            .unwrap_or_else(|| text(&v["content"])),
        _ => String::new(),
    }
}
fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
type Cell = (Value, Option<(String, String)>);

fn card_cell(p: &Value) -> (Value, Cell) {
    let state = &p["state"];
    let name = p["tool"].as_str().or(p["name"].as_str()).unwrap_or("tool");
    let id = p["callID"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or(s(p, "id"));
    let raw = if python::truthy(&p["input"]) {
        &p["input"]
    } else {
        &state["input"]
    };
    let mut args = json!({});
    for (k, v) in raw.as_object().into_iter().flatten() {
        let k = match k.as_str() {
            "filePath" => "file_path",
            "oldString" => "old_string",
            "newString" => "new_string",
            "replaceAll" => "replace_all",
            k => k,
        };
        args[k] = v.clone();
    }
    let status = match s(state, "status") {
        "running" | "pending" => "running",
        "error" => "error",
        _ => "ok",
    };
    let running = status == "running";
    let mut card = tools::card(
        crate::opencode::tool_name(p["name"].as_str().unwrap_or(name)),
        &args,
        id,
    );
    card["status"] = json!(status);
    let mut shown = args.clone();
    for v in shown
        .as_object_mut()
        .into_iter()
        .flat_map(|m| m.values_mut())
    {
        if let Some(t) = v.as_str() {
            *v = json!(cells::clip(t, 400));
        }
    }
    card["input"] = shown;
    let output = state["output"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or(s(&state["metadata"], "output"));
    let mut lines = vec![];
    let mut explored = None;
    let mut cell = match name {
        "bash" => {
            let cmd = s(&args, "command");
            if status != "error" {
                explored = cells::explored(cmd).map(|(l, t)| (l.to_string(), t));
            }
            cells::command(
                &cells::clip(cmd, 400),
                id,
                Some(&json!(output)),
                state["metadata"]["exit"]
                    .as_i64()
                    .or((status == "error").then_some(1)),
                running,
            )
        }
        "read" | "grep" | "glob" => {
            let path = args["file_path"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or(s(&args, "path"));
            let pattern = s(&args, "pattern");
            let label = match name {
                "read" => "Read",
                "grep" => "Search",
                _ => "List",
            };
            let target = if name == "read" {
                path.into()
            } else if path.is_empty() {
                pattern.into()
            } else {
                format!("{pattern} in {path}")
            };
            if status != "error" {
                explored = Some((label.into(), target));
            }
            cells::cell("exploration", "Explored", "", status, id, vec![])
        }
        "edit" | "write" => {
            let path = s(&args, "file_path");
            lines.push(cells::line(
                path,
                if name == "edit" { "Edit" } else { "Add" },
                "detail",
            ));
            for (key, prefix, kind) in [
                ("old_string", "-", "diff_old"),
                (
                    if name == "edit" {
                        "new_string"
                    } else {
                        "content"
                    },
                    "+",
                    "diff_new",
                ),
            ] {
                if name == "write" && key == "old_string" {
                    continue;
                }
                let value =
                    args[key]
                        .as_str()
                        .unwrap_or(if key == "new_string" { "None" } else { "" });
                for line in python::splitlines(value).into_iter().take(40) {
                    lines.push(cells::line(&format!("{prefix}{line}"), "", kind));
                }
            }
            cells::cell("patch", "Edited", path, status, id, lines)
        }
        "websearch" | "webfetch" => {
            let key = if name == "webfetch" { "url" } else { "query" };
            let mut c = cells::web_search(
                id,
                Some(&json!(
                    args[key]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("web")
                )),
                running,
            );
            if name == "webfetch" {
                c["title"] = json!(if running { "Fetching" } else { "Fetched" });
            }
            c["status"] = json!(status);
            c["lines"] = json!([]);
            c
        }
        "todowrite" => {
            for todo in args["todos"].as_array().into_iter().flatten() {
                if let Some(t) = todo["content"].as_str().filter(|s| !s.is_empty()) {
                    lines.push(cells::line(
                        t,
                        &super::agy::title(&s(todo, "status").replace('_', " ")),
                        "status",
                    ));
                }
            }
            cells::cell("plan", "Updated plan", "", status, id, lines)
        }
        "task" => {
            for (key, label, kind, n) in [
                ("prompt", "Task", "detail", 300),
                ("subagent_type", "Type", "muted", usize::MAX),
            ] {
                if let Some(t) = args[key].as_str().filter(|s| !s.is_empty()) {
                    lines.push(cells::line(&cells::clip(t, n), label, kind));
                }
            }
            if !output.is_empty() {
                lines.push(cells::line(&cells::clip(output, 600), "Result", "status"));
            }
            cells::cell(
                "subagents",
                match status {
                    "running" => "Running agent",
                    "error" => "Agent failed",
                    _ => "Agent finished",
                },
                s(&args, "description"),
                status,
                id,
                lines,
            )
        }
        "question" => {
            let first = args["questions"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|v| v.is_object())
                .unwrap_or(&Value::Null);
            for opt in first["options"].as_array().into_iter().flatten() {
                if let Some(t) = opt["label"].as_str().filter(|s| !s.is_empty()) {
                    lines.push(cells::line(t, "Option", "muted"));
                }
            }
            if !output.is_empty() {
                lines.push(cells::line(&cells::clip(output, 300), "Answer", "status"));
            }
            let title = ["question", "header"]
                .iter()
                .find_map(|k| first[k].as_str().filter(|s| !s.is_empty()))
                .unwrap_or("question");
            cells::cell(
                "tool",
                if running { "Asking" } else { "Asked" },
                &cells::clip(title, 240),
                status,
                id,
                lines,
            )
        }
        "skill" => cells::cell("tool", "Loaded skill", s(&args, "name"), status, id, vec![]),
        _ => {
            let args = serde_json::Map::from_iter([("call_id".to_string(), json!(id))]);
            let mut c = cells::generic(name, &args, status);
            c["status"] = json!(status);
            c["lines"] = json!(if output.is_empty() {
                vec![]
            } else {
                cells::preview(&json!(output), 5)
            });
            c
        }
    };
    if let Some(lines) = cell["lines"].as_array_mut() {
        let error = s(state, "error").trim();
        if name != "todowrite" && !error.is_empty() {
            lines.push(cells::line(
                &cells::clip(error.strip_prefix("Error: ").unwrap_or(error), 300),
                "",
                "error",
            ));
        }
        if name == "task"
            && let Some(s) = state["metadata"]["sessionId"]
                .as_str()
                .filter(|s| !s.is_empty())
        {
            lines.push(cells::line(s, "Session", "muted"));
        }
    }
    (card, (cell, explored))
}

#[derive(Default)]
struct Pieces {
    cards: Vec<Value>,
    cells: Vec<Cell>,
}
impl Pieces {
    fn append(&mut self, other: &mut Self) {
        self.cards.append(&mut other.cards);
        self.cells.append(&mut other.cells);
    }
}

pub fn turns(messages: &[(i64, Value, Vec<Value>)]) -> Vec<Value> {
    let mut rows: Vec<(Value, Pieces)> = Vec::new();
    let mut pending = Pieces::default();
    let mut owner: Option<usize> = None;
    for (created, data, stored) in messages {
        let role = data["role"]
            .as_str()
            .or(data["type"].as_str())
            .unwrap_or("")
            .to_lowercase();
        let role = match role.as_str() {
            "user" | "human" => "user",
            "assistant" | "model" | "ai" => "assistant",
            _ => continue,
        };
        let mut pieces = Pieces::default();
        let mut parts = Vec::new();
        for p in data["parts"]
            .as_array()
            .into_iter()
            .flatten()
            .chain(stored)
            .filter(|p| p.is_object())
        {
            match s(p, "type") {
                "tool" | "tool_use" | "toolcall" => {
                    let (c, cell) = card_cell(p);
                    pieces.cards.push(c);
                    pieces.cells.push(cell);
                }
                "" | "text"
                    if !python::truthy(&p["synthetic"]) && !python::truthy(&p["ignored"]) =>
                {
                    let t = text(p);
                    if !t.is_empty() {
                        parts.push(t);
                    }
                }
                _ => {}
            }
        }
        let mut said = text(if python::truthy(&data["content"]) {
            &data["content"]
        } else {
            &data["body"]
        });
        if said.is_empty() {
            said = parts.join("\n\n");
        }
        let mut failed = false;
        if role == "user" {
            said = told::unheaded(&said).into();
        } else if said.is_empty() && pieces.cards.is_empty() {
            let e = &data["error"];
            if e.is_object() && e["name"] != "MessageAbortedError" {
                let name = e["name"].as_str().unwrap_or("error");
                let msg = e["data"]["message"]
                    .as_str()
                    .unwrap_or(s(e, "message"))
                    .trim();
                let why = failures::explain(msg);
                said = if !why.is_empty() {
                    format!("{why}. (OpenCode: {msg})")
                } else if msg.is_empty() {
                    format!("OpenCode {name}")
                } else {
                    format!("OpenCode {name}: {msg}")
                };
                failed = true;
            }
        }
        if said.is_empty() && pieces.cards.is_empty() {
            continue;
        }
        if role == "assistant" && said.is_empty() {
            if let Some(i) = owner {
                rows[i].1.append(&mut pieces);
            } else {
                pending.append(&mut pieces);
            }
            continue;
        }
        if role == "user" && !pending.cards.is_empty() {
            flush_pending(&mut pending, &mut rows);
        }
        if role == "assistant" {
            pending.append(&mut pieces);
            pieces = std::mem::take(&mut pending);
        }
        let ms = if *created > 10_000_000_000 {
            *created
        } else {
            created.saturating_mul(1000)
        };
        let stamp = jiff::Timestamp::from_millisecond(ms)
            .map_or_else(|_| created.to_string(), |t| format!("{t:.3}"));
        let mut row = json!({"role":role,"text":said,"tools":[],"timestamp":stamp});
        if failed {
            row["failed"] = json!(true);
        } else if role == "assistant" && !s(data, "finish").is_empty() {
            row["kind"] = json!(if data["finish"] == "tool-calls" {
                "commentary"
            } else {
                "final_answer"
            });
        }
        owner = (role == "assistant" && !failed).then_some(rows.len());
        rows.push((row, pieces));
    }
    if !pending.cards.is_empty() {
        flush_pending(&mut pending, &mut rows);
    }
    let last = rows.iter().rposition(|(r, _)| r["role"] == "assistant");
    rows.into_iter()
        .enumerate()
        .map(|(i, (mut r, mut p))| {
            if Some(i) != last {
                for c in p.cards.iter_mut().chain(p.cells.iter_mut().map(|(v, _)| v)) {
                    if c["status"] == "running" {
                        c["status"] = json!("recorded");
                        if let Some(title) = match s(c, "title") {
                            "Running" => Some("Ran"),
                            "Exploring" => Some("Explored"),
                            "Running agent" => Some("Agent stopped"),
                            "Fetching" => Some("Fetched"),
                            "Searching" => Some("Searched"),
                            "Asking" => Some("Asked"),
                            "Calling" => Some("Called"),
                            _ => None,
                        } {
                            c["title"] = json!(title);
                        }
                    }
                }
            }
            r["tools"] = json!(p.cards);
            if !p.cells.is_empty() {
                r["display_cells"] = json!(
                    super::agy::gathered(p.cells)
                        .iter()
                        .map(cells::capped)
                        .collect::<Vec<_>>()
                );
            }
            r
        })
        .collect()
}
fn flush_pending(p: &mut Pieces, rows: &mut Vec<(Value, Pieces)>) {
    if let Some((r, pieces)) = rows.last_mut()
        && r["role"] == "assistant"
    {
        pieces.append(p);
        return;
    }
    let stamp = rows
        .last()
        .map_or(json!(""), |(r, _)| r["timestamp"].clone());
    rows.push((
        json!({"role":"assistant","text":"","tools":[],"timestamp":stamp}),
        std::mem::take(p),
    ));
}

fn from_db(con: &Connection, id: &str) -> Option<Vec<Value>> {
    let mut parts: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    if let Ok(mut st) =
        con.prepare("SELECT message_id,data FROM part WHERE session_id=? ORDER BY message_id,id")
    {
        for row in st
            .query_map([id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .ok()?
        {
            let (m, raw) = row.ok()?;
            if let Ok(v) = serde_json::from_str(&raw) {
                parts.entry(m).or_default().push(v);
            }
        }
    }
    let mut st = con
        .prepare(
            "SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created,id",
        )
        .ok()?;
    let mut messages = vec![];
    for row in st
        .query_map([id], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .ok()?
    {
        let (m, t, raw) = row.ok()?;
        if let Ok(v) = serde_json::from_str(&raw) {
            messages.push((t, v, parts.remove(&m).unwrap_or_default()));
        }
    }
    Some(turns(&messages))
}
pub fn read(path: &Path, id: &str) -> Option<Vec<Value>> {
    if !clarp_kernel::native_session_name(id) {
        return None;
    }
    let con = open(path)?;
    con.query_row("SELECT 1 FROM session WHERE id=?", [id], |_| Ok(()))
        .ok()?;
    from_db(&con, id)
}

pub fn sessions(path: &Path, cwd: &str, all: bool, limit: usize) -> Vec<Value> {
    let Some(con) = open(path) else { return vec![] };
    let parent = con
        .query_row(
            "SELECT count(*) FROM pragma_table_info('session') WHERE name='parent_id'",
            [],
            |r| r.get::<_, u32>(0),
        )
        .unwrap_or(0)
        > 0;
    let filter = if parent { " AND parent_id IS NULL" } else { "" };
    let sql = format!(
        "SELECT id,directory,title,time_updated FROM session WHERE time_archived IS NULL {filter} AND (?1 OR directory=?2) ORDER BY time_updated DESC LIMIT ?3"
    );
    let Ok(mut st) = con.prepare(&sql) else {
        return vec![];
    };
    let Ok(rows) = st.query_map(params![all || cwd.is_empty(), cwd, limit], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?.unwrap_or_default(),
            r.get::<_, Option<String>>(2)?.unwrap_or_default(),
            r.get::<_, Option<i64>>(3)?.unwrap_or(0),
        ))
    }) else {
        return vec![];
    };
    rows.flatten().map(|(id,dir,title,mut mtime)|{
        let preview=preview(&con,&id).or_else(||from_db(&con,&id)?.into_iter().find(|r|r["role"]=="user"&&r["text"]!="").map(|r|s(&r,"text").into())).unwrap_or_default();
        if mtime>10_000_000_000{mtime/=1000;}
        json!({"id":id,"mtime":mtime,"preview":python::first(&preview,240),"title":title,"cwd":dir})
    }).collect()
}
fn preview(con: &Connection, id: &str) -> Option<String> {
    let mut st=con.prepare("SELECT p.data FROM part p JOIN message m ON m.id=p.message_id WHERE p.session_id=? AND json_extract(m.data,'$.role')='user' AND json_extract(p.data,'$.type')='text' ORDER BY m.time_created,m.id,p.id LIMIT 5").ok()?;
    st.query_map([id], |r| r.get::<_, String>(0))
        .ok()?
        .flatten()
        .filter_map(|raw| serde_json::from_str::<Value>(&raw).ok())
        .filter(|v| !python::truthy(&v["synthetic"]) && !python::truthy(&v["ignored"]))
        .map(|v| told::unheaded(&text(&v)).trim().into())
        .find(|s: &String| !s.is_empty())
}
