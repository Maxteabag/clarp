//! Server-side producers enqueue timeline records without waiting for persistence.
use crate::edge::App;
use serde_json::{Value, json};
use std::process::Stdio;
use tokio::io::AsyncWriteExt as _;

pub(crate) fn record(app: &App, name: &str, mut fields: Value) {
    if fields["session"].is_null() {
        let dir = app.fleet.directory();
        fields["session"] = json!(
            dir.focus
                .as_deref()
                .and_then(|id| dir.named(id))
                .map_or("claude", |member| member.record.session.as_str())
        );
    }
    let row = clarp_kernel::voice_timeline::server_row(name, fields, app.clock.now_ms());
    app.fleet.record_voice(row);
}

pub(crate) fn metrics(
    app: &App,
    audio: axum::body::Bytes,
    kind: &str,
    session: &str,
    trace: &str,
    utterance: &str,
    empty: bool,
) {
    let app = app.clone();
    let (kind, session, trace, utterance) = (
        kind.to_owned(),
        session.to_owned(),
        trace.to_owned(),
        utterance.to_owned(),
    );
    tokio::spawn(async move {
        let bytes = audio.len();
        let env = &app.config.whisper_python;
        let python = if env.is_empty() { "python3" } else { env };
        let mut cmd = tokio::process::Command::from(clarp_child::command(python));
        cmd.arg(app.config.whisper_worker.with_file_name("voice-metrics.py"))
            .arg(&kind)
            .arg(if empty { "empty" } else { "present" })
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let future = async {
            let mut child = cmd.spawn().ok()?;
            let mut input = child.stdin.take()?;
            input.write_all(&audio).await.ok()?;
            drop(input);
            let output = child.wait_with_output().await.ok()?;
            if !output.status.success() {
                return None;
            }
            serde_json::from_slice::<Value>(&output.stdout).ok()
        };
        let measured=tokio::time::timeout(std::time::Duration::from_secs(30),future).await.ok().flatten()
            .unwrap_or_else(||json!({"metrics":{"bytes":bytes,"content_type":kind,"unavailable":"analyser"},"reasons":[]}));
        let metric = &measured["metrics"];
        record(
            &app,
            "level",
            json!({"session":session,"trace_id":trace,"utterance_id":utterance,
            "duration_ms":metric["duration_ms"],"level_db":metric["rms_db"],"peak_db":metric["peak_db"],"detail":metric}),
        );
        if measured["reasons"]
            .as_array()
            .is_some_and(|r| !r.is_empty())
        {
            record(
                &app,
                "corrupt",
                json!({"session":session,"trace_id":trace,"utterance_id":utterance,
                "detail":{"reasons":measured["reasons"],"metrics":metric}}),
            );
        }
    });
}

/// Capture only requests carrying a voice utterance ID. Most sends do not allocate here.
pub(crate) struct Sent {
    uid: String,
    text: String,
    client_msg_id: String,
    hands_free: bool,
}
impl Sent {
    pub(crate) fn read(req: &clarp_protocol::requests::SendRequest) -> Option<Self> {
        let uid = req
            .utterance_id
            .as_deref()
            .map(clarp_kernel::python::strip)
            .filter(|id| !id.is_empty())
            .or_else(|| {
                req.transcription_id
                    .as_deref()
                    .map(clarp_kernel::python::strip)
                    .filter(|id| !id.is_empty())
            })?;
        Some(Self {
            uid: clarp_kernel::python::first(uid, 128).to_owned(),
            text: clarp_kernel::python::strip(&req.text).to_owned(),
            client_msg_id: req.client_msg_id.as_deref().unwrap_or("").trim().to_owned(),
            hands_free: req.hands_free.unwrap_or(false),
        })
    }
    pub(crate) fn posted(&self, app: &App, response: &clarp_protocol::responses::SendResponse) {
        if !response.ok {
            return;
        }
        let client_msg_id = if self.client_msg_id.is_empty() {
            &response.trace_id
        } else {
            &self.client_msg_id
        };
        let detail = match response.orchestrator.as_ref() {
            Some(action) if action["action"] == "disabled" => return,
            Some(action) => {
                json!({"dispatch":response.dispatch,"client_msg_id":client_msg_id,"orchestrator":action["action"]})
            }
            None => {
                json!({"dispatch":response.dispatch,"client_msg_id":client_msg_id,"queued":response.queued.unwrap_or(false),"hands_free":self.hands_free})
            }
        };
        record(
            app,
            "send",
            json!({"session":response.session,"trace_id":response.trace_id,"utterance_id":self.uid,"text":self.text,"detail":detail}),
        );
    }
}
