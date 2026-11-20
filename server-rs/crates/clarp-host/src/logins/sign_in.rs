//! Signing a tool in and out from an app (the feature `backend_auth`): `GET /backend-auth`,
//! `POST /backend-auth/login`, `/backend-auth/login-code` and `/backend-auth/logout`.
//!
//! The tools keep their logins themselves, one for each machine: the Host runs the tool's own
//! sign-in and sign-out and never reads, writes or says a login. What the app is shown of a
//! sign-in is said by `clarp_kernel::sign_in`, the Python Host's rules.
//!
//! One Host keeps a machine's tools (the lead's ruling of 20 Nov 2026: two Hosts may share a
//! machine, and its tools' logins are one). That is the Host started with `--accounts
//! select`. Every other Host (`follow`, beside the one that keeps them, or no mode) refuses a
//! change here before it reads the request, and starts nothing; what it reads is as before.
//!
//! The sign-ins that run are owned by one task, [`Desk`]'s, and reached by messages: what a
//! tool prints, a code pasted back, the end of the tool. A sign-in lives as long as this Host;
//! its tool is stopped when the Host stops (the Python Host loses it the same way).

use std::collections::BTreeMap;
use std::io::Read as _;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use axum::Json;
use axum::extract::{Query, State};
use axum::http::{StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use clarp_kernel::recovery::Accounts;
use clarp_kernel::sign_in::{self, CodeConflict, State as Stage};
use clarp_ports::clock::Clock;
use serde_json::{Map, Value, json};
use tokio::io::AsyncWriteExt as _;
use tokio::sync::{mpsc, oneshot};

use crate::edge::{ApiError, App};

/// What a Host that does not keep this machine's tools answers a change with.
pub const NOT_KEPT: &str =
    "This Host does not keep this machine's tools; sign the tool in or out on the Host that does.";

/// Whether this Host keeps the tools of its machine: it changes their logins.
pub fn keeps(app: &App) -> bool {
    app.config.accounts == Accounts::Select
}

/// How long a sign-in may run here, in milliseconds: the reference's ten minutes, or what
/// `CLARP_SIGN_IN_MOST_MS` says (for the Host's tests, which cannot wait ten minutes).
fn most_ms() -> i64 {
    std::env::var("CLARP_SIGN_IN_MOST_MS")
        .ok()
        .and_then(|said| said.trim().parse::<i64>().ok())
        .filter(|most| *most > 0)
        .unwrap_or(sign_in::TIMEOUT_MS)
}

// ---- the desk: one task owns the sign-ins ----------------------------------------------------

/// The sign-ins of this Host's tools. Its task starts with the first thing asked of it.
#[derive(Default)]
pub struct Desk {
    orders: OnceLock<mpsc::UnboundedSender<Order>>,
}

enum Order {
    Start {
        tool: String,
        program: String,
        args: Vec<String>,
        reply: oneshot::Sender<Value>,
    },
    Code {
        tool: String,
        code: String,
        reply: oneshot::Sender<Result<Value, CodeConflict>>,
    },
    Told {
        tools: Vec<String>,
        reply: oneshot::Sender<Map<String, Value>>,
    },
    Forget {
        tool: String,
    },
    Printed {
        tool: String,
        run: u64,
        more: String,
    },
    Ended {
        tool: String,
        run: u64,
        ended: Ending,
    },
}

/// How a sign-in's tool ended.
enum Ending {
    /// It ended by itself, well or not.
    Exited { well: bool },
    /// It could not be started, for this reason.
    NotStarted(String),
}

/// One tool's sign-in: the last one started, running or not.
struct Entry {
    run: u64,
    /// What the app is told. Taken away by a sign-out; what the tool prints after makes it
    /// again, as in the Python Host.
    told: Option<Value>,
    printed: String,
    started_at: i64,
    /// What was shown when a code was last pasted back.
    answered: String,
    /// The tool's input, while it runs.
    input: Option<tokio::process::ChildStdin>,
}

impl Entry {
    fn running(&self) -> bool {
        self.input.is_some()
    }
}

impl Desk {
    fn orders(&self, clock: &Arc<dyn Clock + Send + Sync>) -> &mpsc::UnboundedSender<Order> {
        self.orders.get_or_init(|| {
            let (orders, inbox) = mpsc::unbounded_channel();
            tokio::spawn(own(inbox, orders.clone(), clock.clone()));
            orders
        })
    }

    async fn ask<T>(
        &self,
        app: &App,
        order: impl FnOnce(oneshot::Sender<T>) -> Order,
    ) -> Result<T, ApiError> {
        let (reply, answer) = oneshot::channel();
        let sent = self.orders(&app.clock).send(order(reply));
        let gone = || ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "sign-ins unavailable");
        sent.map_err(|_| gone())?;
        answer.await.map_err(|_| gone())
    }

    /// What the app is told of each of these tools' sign-ins: the last one, or one that was
    /// never started.
    pub async fn told(&self, app: &App, tools: Vec<String>) -> Map<String, Value> {
        self.ask(app, |reply| Order::Told { tools, reply })
            .await
            .unwrap_or_default()
    }
}

/// The desk's task: it alone holds the sign-ins.
async fn own(
    mut inbox: mpsc::UnboundedReceiver<Order>,
    orders: mpsc::UnboundedSender<Order>,
    clock: Arc<dyn Clock + Send + Sync>,
) {
    let mut entries: BTreeMap<String, Entry> = BTreeMap::new();
    let mut runs = 0_u64;
    while let Some(order) = inbox.recv().await {
        let now = clock.now_ms();
        match order {
            Order::Start {
                tool,
                program,
                args,
                reply,
            } => {
                if let Some(entry) = entries.get_mut(&tool).filter(|entry| entry.running()) {
                    let told = entry.told.get_or_insert_with(|| {
                        let printed = &entry.printed;
                        let (at, answered) = (entry.started_at, &entry.answered);
                        sign_in::task(&tool, Stage::Running, printed, at, now, "", answered)
                    });
                    let _ = reply.send(told.clone());
                    continue;
                }
                runs += 1;
                let told = sign_in::task(&tool, Stage::Running, "", now, now, "", "");
                let input = start(&tool, runs, &program, &args, &orders);
                let entry = Entry {
                    run: runs,
                    told: Some(told.clone()),
                    printed: String::new(),
                    started_at: now,
                    answered: String::new(),
                    input,
                };
                entries.insert(tool, entry);
                let _ = reply.send(told);
            }
            Order::Code { tool, code, reply } => {
                let answer = match entries.get_mut(&tool) {
                    Some(entry) => give_code(&tool, entry, &code, now).await,
                    None => Err(CodeConflict::NotWaiting),
                };
                let _ = reply.send(answer);
            }
            Order::Told { tools, reply } => {
                let told = tools.into_iter().map(|tool| {
                    let told = entries.get(&tool).and_then(|entry| entry.told.clone());
                    let idle = || sign_in::task(&tool, Stage::Idle, "", 0, now, "", "");
                    let told = told.unwrap_or_else(idle);
                    (tool, told)
                });
                let _ = reply.send(told.collect());
            }
            Order::Forget { tool } => {
                if let Some(entry) = entries.get_mut(&tool) {
                    entry.told = None;
                    entry.answered.clear();
                }
            }
            Order::Printed { tool, run, more } => {
                if let Some(entry) = entries.get_mut(&tool).filter(|entry| entry.run == run) {
                    sign_in::append(&mut entry.printed, &more);
                    let (at, answered) = (entry.started_at, &entry.answered);
                    let printed = &entry.printed;
                    let told = sign_in::task(&tool, Stage::Running, printed, at, now, "", answered);
                    entry.told = Some(told);
                }
            }
            Order::Ended { tool, run, ended } => {
                if let Some(entry) = entries.get_mut(&tool).filter(|entry| entry.run == run) {
                    entry.input = None;
                    entry.answered.clear();
                    let (stage, error) = match ended {
                        Ending::Exited { well: true } => (Stage::Complete, String::new()),
                        Ending::Exited { well: false } => {
                            (Stage::Failed, sign_in::failure(&entry.printed))
                        }
                        Ending::NotStarted(why) => {
                            entry.printed = why;
                            (Stage::Failed, sign_in::failure(&entry.printed))
                        }
                    };
                    let at = entry.started_at;
                    let told = sign_in::task(&tool, stage, &entry.printed, at, now, &error, "");
                    entry.told = Some(told);
                }
            }
        }
    }
}

/// Hands a code to a sign-in that asks for one, and says what the app is told after.
async fn give_code(
    tool: &str,
    entry: &mut Entry,
    code: &str,
    now: i64,
) -> Result<Value, CodeConflict> {
    sign_in::code_can_go(entry.told.as_ref(), entry.running())?;
    let input = entry.input.as_mut().ok_or(CodeConflict::NotAccepting)?;
    let line = format!("{code}\n");
    let written = async {
        input.write_all(line.as_bytes()).await?;
        input.flush().await
    };
    let written = tokio::time::timeout(Duration::from_secs(5), written).await;
    match written {
        Ok(Ok(())) => {}
        Ok(Err(why)) => return Err(CodeConflict::NotSent(why.to_string())),
        Err(_) => return Err(CodeConflict::NotSent("the tool did not read it".into())),
    }
    // The prompt stays in what the tool printed after it read the code: what was shown now
    // is remembered, so the same prompt does not ask again.
    let shown = entry
        .told
        .as_ref()
        .and_then(|told| told["output"].as_str())
        .unwrap_or("")
        .to_string();
    let told = sign_in::task(
        tool,
        Stage::Running,
        &shown,
        entry.started_at,
        now,
        "",
        &shown,
    );
    entry.answered = shown;
    entry.told = Some(told.clone());
    Ok(told)
}

/// Starts a tool's sign-in, its output and its errors in one stream as the Python Host reads
/// them, and the task that reads it and tells the desk. Answers the tool's input; `None`
/// when it could not be started, which the desk is told.
fn start(
    tool: &str,
    run: u64,
    program: &str,
    args: &[String],
    orders: &mpsc::UnboundedSender<Order>,
) -> Option<tokio::process::ChildStdin> {
    let tell = |ended: Ending| {
        let tool = tool.to_string();
        let _ = orders.send(Order::Ended { tool, run, ended });
    };
    let spawned = (|| {
        let (reader, writer) = std::io::pipe()?;
        let mut command = crate::child::asynchronous(program);
        command
            .args(args)
            .stdin(std::process::Stdio::piped())
            .stdout(writer.try_clone()?)
            .stderr(writer)
            .kill_on_drop(true);
        // The command holds the writing end until it is dropped: then the reader ends when
        // the tool does.
        let child = command.spawn();
        drop(command);
        child.map(|child| (child, reader))
    })();
    let (mut child, reader) = match spawned {
        Ok(started) => started,
        Err(why) => {
            tell(Ending::NotStarted(why.to_string()));
            return None;
        }
    };
    let input = child.stdin.take();
    let (tool, orders) = (tool.to_string(), orders.clone());
    tokio::spawn(async move {
        let reading = {
            let (tool, orders) = (tool.clone(), orders.clone());
            tokio::task::spawn_blocking(move || pass_on(reader, &tool, run, &orders))
        };
        let most = Duration::from_millis(u64::try_from(most_ms()).unwrap_or(u64::MAX));
        let status = match tokio::time::timeout(most, child.wait()).await {
            Ok(status) => status.ok(),
            Err(_) => {
                let _ = child.start_kill();
                let more = "\nLogin timed out.".to_string();
                let _ = orders.send(Order::Printed {
                    tool: tool.clone(),
                    run,
                    more,
                });
                tokio::time::timeout(Duration::from_secs(5), child.wait())
                    .await
                    .ok()
                    .and_then(Result::ok)
            }
        };
        // What the tool printed last is read before it is said how it ended; a child of the
        // tool that holds the stream open is not waited for long.
        let _ = tokio::time::timeout(Duration::from_secs(5), reading).await;
        let well = status.is_some_and(|status| status.success());
        let _ = orders.send(Order::Ended {
            tool,
            run,
            ended: Ending::Exited { well },
        });
    });
    input
}

/// Reads what a sign-in's tool prints and tells the desk, as text: a sign cut in two at the
/// end of a read waits for its rest, and what is not UTF-8 is the replacement sign.
fn pass_on(
    mut reader: std::io::PipeReader,
    tool: &str,
    run: u64,
    orders: &mpsc::UnboundedSender<Order>,
) {
    let mut buffer = [0_u8; 4096];
    let mut held: Vec<u8> = Vec::new();
    loop {
        let count = match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(count) => count,
        };
        held.extend_from_slice(&buffer[..count]);
        let whole = match std::str::from_utf8(&held) {
            Ok(_) => held.len(),
            Err(error) if error.error_len().is_none() => error.valid_up_to(),
            Err(_) => held.len(),
        };
        let more = String::from_utf8_lossy(&held[..whole]).into_owned();
        held.drain(..whole);
        if !more.is_empty() {
            let tool = tool.to_string();
            let _ = orders.send(Order::Printed { tool, run, more });
        }
    }
    if !held.is_empty() {
        let more = String::from_utf8_lossy(&held).into_owned();
        let tool = tool.to_string();
        let _ = orders.send(Order::Printed { tool, run, more });
    }
}

// ---- the routes -------------------------------------------------------------------------

/// `GET /backend-auth` (`?harnesses=all` for an app that lists every harness): whether each
/// tool this Host runs is installed and has a login, read off the file the tool keeps it in,
/// and the last sign-in of each tool listed that can be signed in from an app.
pub async fn read(
    State(app): State<App>,
    Query(asked): Query<std::collections::HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let every = asked.get("harnesses").is_some_and(|said| said == "all");
    let mut told = rows(&app, every, true).await?;
    let listed: Vec<String> = told["backends"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row["id"].as_str())
                .filter(|tool| sign_in::driver(tool).is_some())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    told["tasks"] = Value::Object(app.sign_ins.told(&app, listed).await);
    Ok(Json(told))
}

/// The rows of the tools, as `GET /backend-auth` answers them. `verified`: a login whose
/// usage was asked of its provider within the minute is called valid.
async fn rows(app: &App, every: bool, verified: bool) -> Result<Value, ApiError> {
    let now = app.clock.now_ms();
    let keeps = keeps(app);
    let limits = app.limits.clone();
    let ask_usage = app.config.ask_usage && verified;
    let read = move || {
        let home = crate::config::home();
        let mut told = crate::logins::told(&home, every, now);
        if let Some(rows) = told["backends"].as_array_mut() {
            for row in rows.iter_mut() {
                if ask_usage
                    && row["state"] == "unverified"
                    && row["id"]
                        .as_str()
                        .is_some_and(|tool| limits.login_verified(tool, &home, now))
                {
                    row["state"] = json!("valid");
                    row["logged_in"] = json!(true);
                }
                if keeps {
                    signs_in_here(row, every);
                }
            }
        }
        told
    };
    let told = tokio::task::spawn_blocking(read).await;
    let mut told = told.map_err(|_| {
        ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "the logins could not be read",
        )
    })?;
    if let Some(row) = told["backends"].as_array_mut().and_then(|rows| {
        rows.iter_mut()
            .find(|row| row["id"] == "grok" && row["installed"] == true)
    }) {
        crate::logins::probe_grok(row).await;
        if keeps {
            signs_in_here(row, every);
        }
    }
    Ok(told)
}

/// A row of a tool that this Host signs in and out, as the Python Host says it: the app may
/// start a sign-in, and a login that is there can be signed out.
fn signs_in_here(row: &mut Value, every: bool) {
    let Some(tool) = row["id"].as_str() else {
        return;
    };
    if sign_in::driver(tool).is_none() {
        return;
    }
    if every {
        row["can_login"] = json!(true);
        row["note"] = json!("");
    }
    if row["installed"] == true {
        row["can_logout"] = row["credential_present"].clone();
    }
}

/// `POST /backend-auth/login`, `/login-code` and `/logout`. Anything else under the door is no
/// route, as there; a change on a Host that does not keep the tools is refused.
pub async fn change(State(app): State<App>, uri: Uri, body: axum::body::Bytes) -> Response {
    let action = uri.path().strip_prefix("/backend-auth/").unwrap_or("");
    if !matches!(action, "login" | "login-code" | "logout") {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    }
    if !keeps(&app) {
        return ApiError::new(StatusCode::CONFLICT, NOT_KEPT).into_response();
    }
    let said = match serde_json::from_slice::<Value>(&body) {
        _ if body.is_empty() => Map::new(),
        Ok(Value::Object(said)) => said,
        _ => return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response(),
    };
    let tool = sign_in::tool_named(&said);
    let answered = match action {
        "login" => sign_in_from_app(&app, &tool)
            .await
            .map(|told| (StatusCode::ACCEPTED, Json(told)).into_response()),
        "login-code" => {
            let code = sign_in::field(said.get("code"));
            code_from_app(&app, &tool, &code)
                .await
                .map(|told| Json(told).into_response())
        }
        _ => sign_out(&app, &tool)
            .await
            .map(|row| Json(row).into_response()),
    };
    answered.unwrap_or_else(IntoResponse::into_response)
}

/// The program of a tool that can be signed in from an app, when it is on this machine.
fn program_of(tool: &str) -> Result<(sign_in::Driver, String), ApiError> {
    let refused = || ApiError::new(StatusCode::BAD_REQUEST, sign_in::not_installed(tool));
    let driver = sign_in::driver(tool).ok_or_else(refused)?;
    let program = clarp_backend::program(tool).unwrap_or_default();
    if !crate::logins::installed(&program) {
        return Err(refused());
    }
    Ok((driver, program))
}

async fn sign_in_from_app(app: &App, tool: &str) -> Result<Value, ApiError> {
    let (driver, program) = program_of(tool)?;
    let args = driver.login.iter().map(|arg| (*arg).to_string()).collect();
    let tool = tool.to_string();
    app.sign_ins
        .ask(app, |reply| Order::Start {
            tool,
            program,
            args,
            reply,
        })
        .await
}

async fn code_from_app(app: &App, tool: &str, said: &str) -> Result<Value, ApiError> {
    if sign_in::driver(tool).is_none() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            sign_in::not_installed(tool),
        ));
    }
    let code = sign_in::code(said).map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let tool = tool.to_string();
    let answer = app
        .sign_ins
        .ask(app, |reply| Order::Code { tool, code, reply })
        .await?;
    answer.map_err(|conflict| ApiError::new(StatusCode::CONFLICT, conflict.words()))
}

/// Runs the tool's own sign-out and answers its row as it is after; a sign-out the tool
/// refused is refused with the reason it gave.
async fn sign_out(app: &App, tool: &str) -> Result<Value, ApiError> {
    let (driver, program) = program_of(tool)?;
    let mut command = crate::child::asynchronous(&program);
    command
        .args(driver.logout)
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true);
    let most = Duration::from_millis(sign_in::SIGN_OUT_MOST_MS);
    let ran = tokio::time::timeout(most, command.output()).await;
    let conflict = |why: &str| ApiError::new(StatusCode::CONFLICT, sign_in::failure(why));
    let done = match ran {
        Ok(Ok(done)) => done,
        Ok(Err(why)) => return Err(conflict(&why.to_string())),
        Err(_) => return Err(conflict("Sign-out timed out.")),
    };
    if !done.status.success() {
        let said = format!(
            "{}{}",
            String::from_utf8_lossy(&done.stdout),
            String::from_utf8_lossy(&done.stderr)
        );
        return Err(conflict(&said));
    }
    let _ = app.sign_ins.orders(&app.clock).send(Order::Forget {
        tool: tool.to_string(),
    });
    let told = rows(app, true, false).await?;
    let row = told["backends"]
        .as_array()
        .and_then(|rows| rows.iter().find(|row| row["id"] == tool).cloned());
    row.ok_or_else(|| ApiError::new(StatusCode::CONFLICT, sign_in::not_installed(tool)))
}
