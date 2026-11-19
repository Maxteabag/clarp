//! The controller-action subcommands used to define and map gestures from an agent.
use crate::{
    artifact_tool::parse,
    controller_actions as actions,
    edge::App,
    ids,
    tools::{ToolAnswer, ToolCall},
};
use clarp_kernel::python;
use serde_json::{Value, json};

pub(crate) async fn run(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let Some((cmd, args)) = args.split_first() else {
        return ToolAnswer::said(2, "controller-action command required");
    };
    let (valued, flags, names): (&[&str], &[&str], &[&str]) = match cmd.as_str() {
        "list" => (&[], &["--json"], &[]),
        "add" => (
            &["--title", "--http-post", "--agent", "--text", "--body"],
            &["--disabled"],
            &["action_id"],
        ),
        "update" => (
            &["--title", "--http-post", "--agent", "--text", "--body"],
            &["--enable", "--disable"],
            &["action_id"],
        ),
        "remove" | "run" => (&[], &[], &["action_id"]),
        "map" => (&["--device", "--button", "--gesture", "--action"], &[], &[]),
        "unmap" => (&["--device", "--button", "--gesture"], &[], &[]),
        "audit" => (&["--limit"], &[], &[]),
        _ => return ToolAnswer::said(2, "unknown controller-action command"),
    };
    let parsed = match parse(args, valued, flags, names) {
        Ok(p) => p,
        Err(e) => return ToolAnswer::said(2, e),
    };
    let opt = |name| parsed.option(name);
    let required: &[&str] = match cmd.as_str() {
        "add" => &["--title"],
        "map" => &["--button", "--gesture", "--action"],
        "unmap" => &["--button", "--gesture"],
        _ => &[],
    };
    let missing: Vec<_> = required
        .iter()
        .copied()
        .filter(|name| opt(name).is_none())
        .collect();
    if !missing.is_empty() {
        return ToolAnswer::said(
            2,
            format!(
                "the following arguments are required: {}",
                missing.join(", ")
            ),
        );
    }
    let id = parsed.positional.first().map_or("", String::as_str);
    let token = call
        .env
        .get(crate::turn_identity::TURN_ENV)
        .map_or("", String::as_str);
    let actor = match crate::turn_identity::caller(app, token).await {
        Ok(Some(m)) => format!("agent:{}", m.record.session),
        Ok(None) => "administrator".into(),
        Err(_) => return ToolAnswer::said(1, crate::turn_identity::UNAVAILABLE),
    };
    let result: Result<ToolAnswer, (u16, String)> = async {
        let print = |s: String| ToolAnswer::printed(0, s);
        match cmd.as_str() {
            "add" | "update" => {
                let mut data = json!({});
                for (k, key) in [
                    ("--title", "title"),
                    ("--http-post", "url"),
                    ("--agent", "session"),
                    ("--text", "text"),
                ] {
                    if let Some(v) = opt(k) {
                        data[key] = v.into();
                    }
                }
                if opt("--http-post").is_some() {
                    data["type"] = "http_post".into();
                }
                if opt("--agent").is_some() {
                    data["type"] = "agent_prompt".into();
                }
                if let Some(raw) = opt("--body") {
                    let body = match serde_json::from_str::<Value>(raw) {
                        Ok(v) if v.is_object() => v,
                        Ok(_) => return Ok(ToolAnswer::said(1, "--body must be a JSON object")),
                        Err(e) => {
                            return Ok(ToolAnswer::said(
                                1,
                                format!("--body is not valid JSON: {e}"),
                            ));
                        }
                    };
                    data["body"] = body;
                }
                let create = cmd == "add";
                if create {
                    if data.get("type").is_none() {
                        return Ok(ToolAnswer::said(
                            1,
                            "add needs --http-post URL or --agent SESSION",
                        ));
                    }
                    data["enabled"] = json!(!parsed.flags.contains("--disabled"));
                } else {
                    if parsed.flags.contains("--enable") || parsed.flags.contains("--disable") {
                        data["enabled"] = json!(parsed.flags.contains("--enable"));
                    }
                    if data == json!({}) {
                        return Ok(ToolAnswer::said(1, "nothing to update"));
                    }
                }
                data["id"] = id.into();
                actions::change(app, if create { "create" } else { "update" }, &data, &actor)
                    .await?;
                Ok(print(format!(
                    "✓ {} controller action {id}",
                    if create { "Created" } else { "Updated" }
                )))
            }
            "remove" => {
                let result = actions::change(app, "delete", &json!({"id":id}), &actor).await?;
                Ok(print(format!(
                    "✓ Removed {id} ({} gesture mapping(s) cleared)",
                    result["mappings_removed"]
                )))
            }
            "map" | "unmap" => {
                let device = opt("--device").unwrap_or("*");
                let button = opt("--button").unwrap_or("");
                let gesture = opt("--gesture").unwrap_or("");
                if !clarp_kernel::controller_actions::BUTTONS.contains(&button) {
                    return Ok(ToolAnswer::said(2, "--button must be primary or secondary"));
                }
                let target = opt("--action");
                actions::change(
                    app,
                    "mappings",
                    &json!({"device":device,"gesture":gesture,"button":button,"action_id":target}),
                    &actor,
                )
                .await?;
                Ok(print(format!(
                    "✓ {button} {gesture} on {device} → {}",
                    target.unwrap_or("its voice behaviour")
                )))
            }
            "run" => {
                let v = actions::invoke(
                    app,
                    id.into(),
                    json!({"request_id":format!("clarp-admin-{}",ids::hex(8))}),
                    actor,
                )
                .await?;
                Ok(ToolAnswer::printed(
                    if v["ok"] == true { 0 } else { 1 },
                    serde_json::to_string_pretty(&v).unwrap_or_default(),
                ))
            }
            "audit" => {
                let limit = opt("--limit").unwrap_or("30");
                if python::int(limit).is_none() {
                    return Ok(ToolAnswer::said(2, "--limit must be an integer"));
                }
                let v = actions::audit(app, limit).await?;
                let mut lines = Vec::new();
                for r in v["entries"].as_array().into_iter().flatten() {
                    let date = jiff::Timestamp::from_millisecond(r["at"].as_i64().unwrap_or(0))
                        .map(|ts| {
                            ts.to_zoned(jiff::tz::TimeZone::system())
                                .strftime("%Y-%m-%d %H:%M:%S")
                                .to_string()
                        })
                        .unwrap_or_default();
                    let event = r["event"].as_str().unwrap_or("");
                    let outcome = if event != "run" {
                        String::new()
                    } else if r["ok"] == true {
                        " ok".into()
                    } else {
                        format!(" FAILED {}", r["error"].as_str().unwrap_or(""))
                    };
                    lines.push(format!(
                        "{date} {event:<6} {:<24} {}{outcome}",
                        r["action_id"].as_str().unwrap_or(""),
                        r["actor"].as_str().unwrap_or("")
                    ));
                }
                Ok(if lines.is_empty() {
                    ToolAnswer::code(0)
                } else {
                    print(lines.join("\n"))
                })
            }
            _ => {
                let state = actions::list(app).await?;
                if parsed.flags.contains("--json") {
                    return Ok(print(
                        serde_json::to_string_pretty(&state).unwrap_or_default(),
                    ));
                }
                let mut lines = Vec::new();
                for a in state["actions"].as_array().into_iter().flatten() {
                    let s = |k: &str| a[k].as_str().unwrap_or("");
                    let flag = if a["enabled"] == false {
                        " (disabled)"
                    } else {
                        ""
                    };
                    let target = if s("type") == "http_post" {
                        format!("POST {} {}", s("url"), python::dumps_ascii(&a["body"]))
                    } else {
                        format!("prompt {}: {}", s("session"), s("text"))
                    };
                    lines.push(format!(
                        "• {}{flag} — {}\n    {target}",
                        s("id"),
                        s("title")
                    ));
                    for m in state["mappings"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter(|m| m["action_id"] == a["id"])
                    {
                        let device = m["device"].as_str().unwrap_or("*");
                        let prefix = if device == "*" {
                            String::new()
                        } else {
                            format!("[{device}] ")
                        };
                        lines.push(format!(
                            "    ← {prefix}{} {}",
                            m["button"].as_str().unwrap_or(""),
                            m["gesture"].as_str().unwrap_or("")
                        ));
                    }
                }
                Ok(print(if lines.is_empty() {
                    "No controller actions.".into()
                } else {
                    lines.join("\n")
                }))
            }
        }
    }
    .await;
    result.unwrap_or_else(|(_, why)| {
        ToolAnswer::said(1, format!("clarp-admin controller-action {cmd}: {why}"))
    })
}
