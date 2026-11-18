//! Each administrative request owns one transaction. Readers see either side
//! of a pack deletion or profile reassignment, including all its related rows.
use std::path::Path;
use std::time::Duration;

use axum::http::StatusCode;
use rusqlite::{Connection, OptionalExtension, Params, params};
use serde_json::{Value, json};

pub(super) type Failure = (StatusCode, String);
pub(super) type Result<T> = std::result::Result<T, Failure>;
pub(super) fn bad(s: &str) -> Failure {
    (StatusCode::BAD_REQUEST, s.into())
}
fn missing(s: &str) -> Failure {
    (StatusCode::NOT_FOUND, s.into())
}
pub(super) fn unavailable(_: rusqlite::Error) -> Failure {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "vocabulary store unavailable".into(),
    )
}
fn constraint(e: rusqlite::Error, expected: Failure) -> Failure {
    if e.sqlite_error_code() == Some(rusqlite::ErrorCode::ConstraintViolation) {
        expected
    } else {
        unavailable(e)
    }
}
pub(super) fn open(root: &Path) -> Result<Connection> {
    let conn = Connection::open(root.join("vocabulary.sqlite")).map_err(unavailable)?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(unavailable)?;
    conn.execute_batch("PRAGMA foreign_keys=ON;")
        .map_err(unavailable)?;
    if conn
        .pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
        .map_err(unavailable)?
        == 0
    {
        conn.execute_batch(concat!(
            "BEGIN IMMEDIATE;",
            include_str!("schema.sql"),
            "PRAGMA user_version=1; COMMIT;"
        ))
        .map_err(unavailable)?;
    }
    Ok(conn)
}
pub(super) fn rows(db: &Connection, sql: &str, args: impl Params) -> Result<Vec<Value>> {
    let mut st = db.prepare(sql).map_err(unavailable)?;
    let strings = st
        .query_map(args, |r| r.get::<_, String>(0))
        .map_err(unavailable)?;
    strings
        .map(|s| {
            serde_json::from_str(&s.map_err(unavailable)?).map_err(|_| {
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "invalid vocabulary record".into(),
                )
            })
        })
        .collect()
}
pub(super) fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        v if !clarp_kernel::python::truthy(v) => String::new(),
        v => clarp_kernel::python::repr(v),
    }
}
fn required(v: &Value, k: &str) -> Result<String> {
    let val = text(&v[k]);
    if val.is_empty() {
        Err(bad(&format!("{k} is required")))
    } else {
        Ok(val)
    }
}
fn number(v: &Value, default: f64) -> Result<f64> {
    if v.is_null() {
        return Ok(default);
    }
    v.as_f64()
        .or_else(|| v.as_bool().map(|b| u8::from(b) as f64))
        .or_else(|| v.as_str()?.trim().parse().ok())
        .filter(|n: &f64| n.is_finite())
        .ok_or_else(|| bad("a finite number is required"))
}
fn integer(v: &Value, default: i64) -> Result<i64> {
    if v.is_null() {
        return Ok(default);
    }
    v.as_i64()
        .or_else(|| v.as_f64().map(|n| n as i64))
        .or_else(|| v.as_bool().map(i64::from))
        .or_else(|| clarp_kernel::python::int(v.as_str()?)?.try_into().ok())
        .ok_or_else(|| bad("an integer is required"))
}
fn term_id(v: &Value) -> Result<i64> {
    v.get("term_id")
        .filter(|v| !v.is_null())
        .and_then(|v| integer(v, 0).ok())
        .ok_or_else(|| bad("term_id is required"))
}

fn add(db: &Connection, pack: &str, term: &Value, default: f64, now: i64) -> Result<bool> {
    let raw = term
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(|| text(&term["text"]));
    let word = raw.trim();
    if word.is_empty() {
        return Ok(false);
    }
    let changed=db.execute("INSERT OR IGNORE INTO term(pack_id,text,say_as,often_heard_as,rarity,source,created_at) VALUES(?,?,?,?,?,?,?)",
        params![pack,word,text(&term["say_as"]).trim(),text(&term["often_heard_as"]).trim(),number(&term["rarity"],default)?,term["source"].as_str().filter(|s|!s.is_empty()).unwrap_or("manual"),now])
        .map_err(|e|constraint(e,missing("no such pack")))?;
    Ok(changed > 0)
}
pub(super) fn change(
    db: &Connection,
    action: &str,
    v: &Value,
    now: i64,
    known_agent: bool,
) -> Result<Value> {
    let ok = json!({"ok":true});
    match action {
        "packs" | "profiles" => {
            let name = text(&v["name"]);
            let name = name.trim();
            if name.is_empty() {
                return Err(bad("name is required"));
            }
            if action == "profiles" {
                let id = format!("pf-{}", crate::ids::hex(6));
                db.execute(
                    "INSERT INTO profile(profile_id,name) VALUES(?,?)",
                    params![id, name],
                )
                .map_err(|e| {
                    constraint(
                        e,
                        (
                            StatusCode::CONFLICT,
                            "a profile with that name exists".into(),
                        ),
                    )
                })?;
                for (pos, pack) in v["pack_ids"].as_array().into_iter().flatten().enumerate() {
                    db.execute(
                        "INSERT OR REPLACE INTO member VALUES(?,?,?)",
                        params![id, text(pack), pos as i64],
                    )
                    .map_err(|e| constraint(e, missing("no such profile or pack")))?;
                }
                return Ok(json!({"ok":true,"profile_id":id}));
            }
            let kind = text(&v["kind"]);
            let kind = if kind.is_empty() { "static" } else { &kind };
            if !matches!(kind, "static" | "dynamic") {
                return Err(bad("kind must be static or dynamic"));
            }
            let id = format!("pk-{}", crate::ids::hex(6));
            db.execute(
                "INSERT INTO pack VALUES(?,?,?,?,?,?,?)",
                params![
                    id,
                    name,
                    kind,
                    text(&v["generator"]),
                    number(&v["priority"], 1.0)?,
                    integer(&v["floor"], 0)?,
                    v.get("enabled").is_none_or(clarp_kernel::python::truthy)
                ],
            )
            .map_err(|e| {
                constraint(
                    e,
                    (StatusCode::CONFLICT, "a pack with that name exists".into()),
                )
            })?;
            for term in v["terms"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|t| t.is_string() || t.is_object())
            {
                add(db, &id, term, 0.5, now)?;
            }
            Ok(json!({"ok":true,"pack_id":id}))
        }
        "packs/update" => {
            let id = required(v, "pack_id")?;
            let optional_number = |key: &str| {
                v.get(key)
                    .filter(|v| !v.is_null())
                    .map(|x| number(x, 0.0))
                    .transpose()
            };
            let name = text(&v["name"]);
            db.execute("UPDATE pack SET name=COALESCE(NULLIF(?,''),name), priority=COALESCE(?,priority),floor=COALESCE(?,floor),enabled=COALESCE(?,enabled) WHERE pack_id=?",
                params![name.trim(),optional_number("priority")?,v.get("floor").filter(|v|!v.is_null()).map(|x|integer(x,0)).transpose()?,v.get("enabled").map(clarp_kernel::python::truthy),id])
                .map_err(|e|constraint(e,(StatusCode::CONFLICT,"a pack with that name exists".into())))?;
            Ok(ok)
        }
        "packs/delete" | "profiles/delete" => {
            let (sql, k) = if action == "packs/delete" {
                ("DELETE FROM pack WHERE pack_id=?", "pack_id")
            } else {
                ("DELETE FROM profile WHERE profile_id=?", "profile_id")
            };
            db.execute(sql, [required(v, k)?]).map_err(unavailable)?;
            Ok(ok)
        }
        "terms" => {
            let pack = text(&v["pack_id"]);
            let term = text(&v["text"]);
            if pack.is_empty() || term.trim().is_empty() {
                return Err(bad("pack_id and text are required"));
            }
            Ok(
                json!({"ok":true,"added":add(db,&pack,v,clarp_kernel::vocabulary::rarity(&term),now)?}),
            )
        }
        "terms/update" => {
            let id = term_id(v)?;
            let name = text(&v["text"]);
            let field = |k: &str| {
                v.get(k)
                    .filter(|v| !v.is_null())
                    .map(|v| text(v).trim().to_string())
            };
            let rarity = v
                .get("rarity")
                .filter(|v| !v.is_null())
                .map(|v| number(v, 0.0).map(|n| n.clamp(0.0, 1.0)))
                .transpose()?;
            let changes = !name.trim().is_empty()
                || field("say_as").is_some()
                || field("often_heard_as").is_some()
                || rarity.is_some();
            let updated=changes && db.execute("UPDATE term SET text=COALESCE(NULLIF(?,''),text),say_as=COALESCE(?,say_as),often_heard_as=COALESCE(?,often_heard_as),rarity=COALESCE(?,rarity) WHERE term_id=?",params![name.trim(),field("say_as"),field("often_heard_as"),rarity,id])
                .map_err(|e|constraint(e,(StatusCode::CONFLICT,"that term already exists in the pack".into())))?>0;
            Ok(json!({"ok":true,"updated":updated}))
        }
        "terms/delete" => {
            db.execute("DELETE FROM term WHERE term_id=?", [term_id(v)?])
                .map_err(unavailable)?;
            Ok(ok)
        }
        "profiles/packs" | "profiles/packs/remove" => {
            let (pf, pk) = (text(&v["profile_id"]), text(&v["pack_id"]));
            if pf.is_empty() || pk.is_empty() {
                return Err(bad("profile_id and pack_id are required"));
            }
            if action.ends_with("/remove") {
                db.execute(
                    "DELETE FROM member WHERE profile_id=? AND pack_id=?",
                    params![pf, pk],
                )
                .map_err(unavailable)?;
            } else {
                db.execute(
                    "INSERT OR REPLACE INTO member VALUES(?,?,?)",
                    params![pf, pk, integer(&v["position"], 0)?],
                )
                .map_err(|e| constraint(e, missing("no such profile or pack")))?;
            }
            Ok(ok)
        }
        "profiles/workspace" => {
            let pf = required(v, "profile_id")?;
            let enabled = v["enabled"]
                .as_bool()
                .ok_or_else(|| bad("enabled must be a boolean"))?;
            if db
                .execute(
                    "UPDATE profile SET harvests=? WHERE profile_id=?",
                    params![enabled, pf.trim()],
                )
                .map_err(unavailable)?
                == 0
            {
                return Err(missing("no such profile"));
            }
            Ok(json!({"ok":true,"profile_id":pf.trim(),"harvests_workspace":enabled}))
        }
        "assign" | "unassign" => {
            let a = text(&v["agent_id"]);
            let t = text(&v["team_id"]);
            let (a, t) = (a.trim(), t.trim());
            if action == "assign" {
                let pf = required(v, "profile_id")?;
                if a.is_empty() == t.is_empty() {
                    return Err(bad("assign to exactly one of agent_id or team_id"));
                }
                if !a.is_empty() && !known_agent {
                    return Err(missing("no such profile"));
                }
                db.execute(
                    "INSERT OR REPLACE INTO assignment VALUES(?,NULLIF(?,''),NULLIF(?,''),?)",
                    params![pf, a, t, now],
                )
                .map_err(|e| constraint(e, missing("no such profile")))?;
            } else {
                if a.is_empty() && t.is_empty() {
                    return Err(bad("agent_id or team_id is required"));
                }
                db.execute(
                    "DELETE FROM assignment WHERE agent_id=? OR team_id=?",
                    params![a, t],
                )
                .map_err(unavailable)?;
            }
            Ok(ok)
        }
        _ => Err(missing("not found")),
    }
}

pub(super) fn read(db: &Connection, action: &str, v: &Value) -> Result<Value> {
    match action {
        "packs" => Ok(
            json!({"packs":rows(db,"SELECT json_object('pack_id',pack_id,'name',name,'kind',kind,'generator',generator,'priority',priority,'floor',floor,'enabled',json(iif(enabled,'true','false')),'terms',(SELECT count(*) FROM term t WHERE t.pack_id=p.pack_id)) FROM pack p ORDER BY name",[])?}),
        ),
        "terms" => {
            let id = required(v, "pack_id")?;
            Ok(
                json!({"pack_id":id,"terms":rows(db,"SELECT json_object('term_id',term_id,'text',text,'say_as',say_as,'often_heard_as',often_heard_as,'rarity',rarity,'source',source,'created_at',created_at) FROM term WHERE pack_id=? ORDER BY rarity DESC,text COLLATE NOCASE",[&id])?}),
            )
        }
        "profiles" => Ok(
            json!({"profiles":rows(db,"SELECT json_object('profile_id',profile_id,'name',name,'packs',(SELECT count(*) FROM member m WHERE m.profile_id=p.profile_id),'assignments',(SELECT count(*) FROM assignment a WHERE a.profile_id=p.profile_id)) FROM profile p ORDER BY name",[])?}),
        ),
        "profile" => {
            let id = text(&v["profile_id"]);
            let (name, harvests): (String, bool) = db
                .query_row(
                    "SELECT name,harvests FROM profile WHERE profile_id=?",
                    [&id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()
                .map_err(unavailable)?
                .ok_or_else(|| missing("no such profile"))?;
            Ok(
                json!({"profile_id":id,"name":name,"harvests_workspace":harvests,
                "packs":rows(db,"SELECT json_object('pack_id',pack_id,'position',position,'name',name,'kind',kind,'enabled',json(iif(enabled,'true','false'))) FROM member JOIN pack USING(pack_id) WHERE profile_id=? ORDER BY position,name",[&id])?,
                "assignments":rows(db,"SELECT json_object('agent_id',coalesce(agent_id,''),'team_id',coalesce(team_id,'')) FROM assignment WHERE profile_id=? ORDER BY rowid",[&id])?}),
            )
        }
        "assignments" => Ok(
            json!({"assignments":rows(db,"SELECT json_object('profile_id',profile_id,'profile_name',name,'agent_id',coalesce(agent_id,''),'team_id',coalesce(team_id,'')) FROM assignment JOIN profile USING(profile_id) ORDER BY created_at,assignment.rowid",[])?}),
        ),
        "runs" | "run" => {
            let one = action == "run";
            let trace = text(&v["trace_id"]);
            let session = text(&v["session"]);
            let limit = integer(&v["limit"], 20).unwrap_or(20).clamp(1, 200);
            let select = "SELECT json_object('run_id',run_id,'session',session,'provider',provider,'model',model,'unit',audit->>'$.unit','capacity',audit->>'$.capacity','used',audit->>'$.used','form',audit->>'$.form','payload',audit->>'$.payload','included',json(audit->'$.included'),'dropped',json(audit->'$.dropped'),'transcript',transcript,'created_at',created_at) FROM run";
            if one {
                if trace.is_empty() {
                    return Err(missing("no run for that trace"));
                }
                let mut found = rows(
                    db,
                    &format!("{select} WHERE trace_id=? ORDER BY run_id DESC LIMIT 1"),
                    [trace],
                )?
                .pop()
                .ok_or_else(|| missing("no run for that trace"))?;
                found["audio_url"] = Value::Null;
                Ok(found)
            } else {
                let list = if session.is_empty() {
                    rows(
                        db,
                        &format!("{select} ORDER BY run_id DESC LIMIT ?"),
                        [limit],
                    )?
                } else {
                    rows(
                        db,
                        &format!("{select} WHERE session=? ORDER BY run_id DESC LIMIT ?"),
                        params![session, limit],
                    )?
                };
                Ok(json!({"runs":list}))
            }
        }
        _ => Err(missing("not found")),
    }
}
