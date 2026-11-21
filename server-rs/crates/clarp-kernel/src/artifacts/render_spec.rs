//! Render specs: a small form an agent declares and the app draws with its own views, from
//! a closed catalog of components, answered once.
//!
//! The rules are the Python Host's (`server/lib/render_spec.py`, `render_spec_store.py`,
//! Host contract 102), held to it by `tests/fixtures/render-spec-vectors.json`
//! (`server-rs/tools/render-spec-vectors.py`). A refusal is a code from a closed list, a
//! pointer and one sentence of the catalog's refusals file; it never carries a value of the
//! payload, and a pointer names a key the caller wrote only when that key passes the
//! identifier rule.
//!
//! The spec is never in a row of a list, a search, an inbox or an event: an app that knows
//! the kind (at or above the threshold the owner set) is told what was left out
//! (`content_omitted`), and reads the spec from the artifact itself; any other app sees a
//! plain row and cannot answer.

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::forms::Submission;
use super::{Artifact, ArtifactBook, Ctx, Maker, New, Opening, Refused, Why, clip};
use clarp_speech::markup::strip_hidden_blocks;

pub mod catalog;
pub mod json;
pub mod validate;

pub use catalog::{Catalog, Ident, Unavailable};
pub use json::{J, Obj, Text};
pub use validate::validate;

use validate::Checks;

/// The kind.
pub const KIND: &str = "render_spec";
/// What a binding of a choice names: the bound key is a list of texts equal to the values
/// of the choice's options, in their order.
pub const OPTION_ORDER: &str = "string_enum_in_option_order";
/// The one refusal whose sentence is not read from the refusals file, since it is what is
/// said when that file cannot be read. The same sentence as the file's.
pub const CATALOG_UNAVAILABLE: &str = "catalog_unavailable";
pub const CATALOG_UNAVAILABLE_SENTENCE: &str = "The form catalog is not available on this Host.";
/// The most bytes answers may have, written canonically.
pub const ANSWERS_MOST: usize = 16_384;

/// Every code a render spec is refused with: the refusals file must give each a sentence.
pub const CODES: [&str; 34] = [
    "client_contract",
    "invalid_object",
    "unknown_key",
    "missing_key",
    "invalid_json",
    "payload_size",
    "unknown_catalog",
    "catalog_version",
    "invalid_version",
    "spec_size",
    "state_size",
    "answer_size",
    "element_count",
    "element_id",
    "element_tree",
    "depth",
    "component",
    "prop",
    "expression",
    "submit_intent",
    "submit_label",
    "schema_keyword",
    "schema_shape",
    "state_value",
    "intent_mismatch",
    "invalid_submission_id",
    "audio_preference",
    "reused_id",
    "reused_id_audio",
    "version_mismatch",
    "already_answered",
    "not_open",
    "number_form",
    CATALOG_UNAVAILABLE,
];

/// The keys the contract itself names: a pointer names these, indexes, and keys that pass
/// the identifier rule.
pub const STRUCTURAL_KEYS: [&str; 26] = [
    "type",
    "title",
    "summary",
    "version",
    "catalog",
    "catalog_version",
    "spec",
    "submit",
    "answer_schema",
    "root",
    "elements",
    "state",
    "props",
    "children",
    "properties",
    "required",
    "enum",
    "minLength",
    "maxLength",
    "intent",
    "label",
    "submission_id",
    "answers",
    "synthesize_audio",
    "$state",
    "$bindState",
];

/// What a request to `/artifacts` or `/decisions` is answered when a text of its body is one
/// no store can hold (`request_texts.SENTENCE`, R79).
pub const UNSTORABLE: &str = "A text in this request holds a character that cannot be stored.";

/// The texts of a publication or a revision besides its payload, in the order they are
/// checked, by the request's own keys.
pub const ENVELOPE_TEXTS: [&str; 6] = [
    "session",
    "title",
    "summary",
    "status",
    "reference_id",
    "artifact_id",
];

/// Every envelope text given is text that UTF-8 can carry, checked before anything else
/// reads it (`render_spec.envelope`, the reference's PR 438: R71 and R72). A value that is
/// no text, null among them, or a text with half a pair is refused at its field.
pub fn envelope(given: &[(&str, &J)]) -> Result<(), Refusal> {
    for key in ENVELOPE_TEXTS {
        let found = given.iter().find(|(name, _)| *name == key);
        if let Some((_, value)) = found
            && !matches!(value, J::Text(Text::Valid(_)))
        {
            return refuse("invalid_json", &format!("/{key}"));
        }
    }
    Ok(())
}

/// A refusal: its code and the pointer to what it is about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub code: &'static str,
    pub path: String,
}

impl Refusal {
    pub fn new(code: &'static str, path: &str) -> Self {
        Self {
            code,
            path: path.to_string(),
        }
    }

    /// The refusal as the rules of artifacts carry one.
    pub fn refused(self) -> Refused {
        Refused {
            why: Why::Spec(self.code),
            message: self.path,
        }
    }

    /// The refusal a refusal of the rules of artifacts carries, when it is one.
    pub fn of(refused: &Refused) -> Option<Self> {
        match refused.why {
            Why::Spec(code) => Some(Self::new(code, &refused.message)),
            _ => None,
        }
    }

    /// What a client is told: the code, the pointer and the sentence of the catalog's
    /// refusals file. While the catalog cannot be loaded every refusal is that one.
    pub fn triple(&self, catalog: Result<&Catalog, Unavailable>) -> Value {
        match catalog {
            Ok(catalog) => json!({
                "code": self.code,
                "path": self.path,
                "message": catalog.reason(self.code),
            }),
            _ => json!({
                "code": CATALOG_UNAVAILABLE,
                "path": "",
                "message": CATALOG_UNAVAILABLE_SENTENCE,
            }),
        }
    }

    /// The status a refusal is answered with.
    pub fn status(code: &str) -> u16 {
        match code {
            CATALOG_UNAVAILABLE => 503,
            "client_contract" => 403,
            "reused_id" | "reused_id_audio" | "version_mismatch" | "already_answered"
            | "not_open" => 409,
            _ => 400,
        }
    }
}

fn refuse<T>(code: &'static str, path: &str) -> Result<T, Refusal> {
    Err(Refusal::new(code, path))
}

fn hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// What a spec is, by its version, its length and its digest (`content_identity`).
pub fn content_identity(catalog: &Catalog, spec: &J, version: &str) -> Result<Value, Refusal> {
    let raw = json::canonical(spec, "/spec", Some(&catalog.ident))?;
    Ok(json!({
        "version": version,
        "bytes": raw.len(),
        "sha256": format!("c1:{}", hex(raw.as_bytes())),
    }))
}

/// The payload as it is kept: as it came, with what it is, how many elements it has, and
/// the catalog it was checked by. For a payload that passed [`validate`].
pub fn stored(catalog: &Catalog, payload: &J) -> Result<Map<String, Value>, Refusal> {
    let spec = payload.get("spec").unwrap_or(&J::Null);
    let version = payload.get("version").and_then(J::str).unwrap_or_default();
    let identity = content_identity(catalog, spec, version)?;
    let count = spec.get("elements").and_then(J::object).map_or(0, Obj::len);
    let Value::Object(mut kept) = payload.to_value() else {
        return refuse("invalid_object", "");
    };
    kept.insert("content_identity".to_string(), identity);
    kept.insert("element_count".to_string(), json!(count));
    kept.insert("catalog_sha256".to_string(), json!(catalog.sha256));
    Ok(kept)
}

/// Whether the client that sent this header can answer a render spec: it says who it is as
/// `<platform>/<build> contract=<n>` exactly, and its contract is at least the threshold the
/// owner set (`server_identity.render_spec_capable`). Without a threshold nobody can.
pub fn capable(header: Option<&str>, minimum: Option<i64>) -> bool {
    let (Some(header), Some(minimum)) = (header, minimum.filter(|minimum| *minimum >= 1)) else {
        return false;
    };
    crate::client::named(header).is_some_and(|(_, contract)| contract >= minimum)
}

/// Whether a submission id is one (`[A-Za-z0-9-]{16,80}`).
fn submission_id(text: &str) -> bool {
    (16..=80).contains(&text.len())
        && text
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

/// A submission made before under the id that comes again.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Previous<'a> {
    pub payload_hash: &'a str,
    pub synthesize_audio: bool,
}

/// What answers to a render spec come to.
#[derive(Debug, Clone, PartialEq)]
pub enum Admitted {
    /// The same answers under the same id: answered as they were.
    Repeat,
    /// Answers nobody sent before, to be kept.
    New {
        payload_hash: String,
        /// The answers, written canonically: as they are told to the agent.
        answers_json: String,
        synthesize_audio: bool,
    },
}

/// Whether answers are taken (`render_spec.admit`): `payload` and `status` are the render
/// spec's, `previous` the submission under the same id when there is one.
pub fn admit(
    catalog: &Catalog,
    artifact_id: &str,
    payload: &J,
    status: &str,
    data: &J,
    previous: Option<Previous<'_>>,
) -> Result<Admitted, Refusal> {
    let checks = Checks { catalog };
    let fields = checks.closed(
        data,
        "",
        &["submission_id", "version", "answers", "synthesize_audio"],
        &["submission_id", "version", "answers"],
        "unknown_key",
    )?;
    let field = |name: &str| fields.get(name).unwrap_or(&J::Null);
    if !field("submission_id").str().is_some_and(submission_id) {
        return refuse("invalid_submission_id", "/submission_id");
    }
    let version = field("version");
    if !matches!(version, J::Text(_)) {
        return refuse("invalid_version", "/version");
    }
    let answers = field("answers");
    if checks.canonical(answers, "/answers")?.len() > ANSWERS_MOST {
        return refuse("answer_size", "/answers");
    }
    let parts = ["intent", "state"];
    let given = checks.closed(answers, "/answers", &parts, &parts, "unknown_key")?;
    let audio = match fields.get("synthesize_audio") {
        None => true,
        Some(J::Bool(audio)) => *audio,
        Some(_) => return refuse("audio_preference", "/synthesize_audio"),
    };
    let weighed = J::Array(vec![
        J::Text(Text::from(artifact_id)),
        version.clone(),
        answers.clone(),
    ]);
    let digest = hex(checks.canonical(&weighed, "")?.as_bytes());
    if let Some(previous) = previous {
        if previous.payload_hash != digest {
            return refuse("reused_id", "/submission_id");
        }
        if fields.contains("synthesize_audio") && previous.synthesize_audio != audio {
            return refuse("reused_id_audio", "/synthesize_audio");
        }
        return Ok(Admitted::Repeat);
    }
    if status == "completed" {
        return refuse("already_answered", "");
    }
    if !matches!(status, "ready" | "active") {
        return refuse("not_open", "");
    }
    if Some(version) != payload.get("version") {
        return refuse("version_mismatch", "/version");
    }
    let declared = payload
        .get("submit")
        .and_then(|submit| submit.get("intent"));
    if given.get("intent") != declared {
        return refuse("intent_mismatch", "/answers/intent");
    }
    let empty = Obj::new();
    let schema = payload
        .get("answer_schema")
        .and_then(J::object)
        .unwrap_or(&empty);
    let state = given.get("state").unwrap_or(&J::Null);
    checks.state(state, schema, "/answers/state", true)?;
    Ok(Admitted::New {
        payload_hash: digest,
        answers_json: checks.canonical(answers, "")?,
        synthesize_audio: audio,
    })
}

/// What the agent is told when its user answered (`render_spec.message_text`): the title as
/// a JSON text, so that every Host writes the same bytes.
pub fn message_text(
    title: &str,
    artifact_id: &str,
    version: &str,
    submission_id: &str,
    answers_json: &str,
) -> String {
    let mut quoted = String::new();
    json::quoted(title, &mut quoted);
    format!(
        "The user answered {quoted}.\nArtifact ID: {artifact_id}\nVersion: {version}\n\
         Submission ID: {submission_id}\nThese are user preferences for review, not approval \
         to perform protected actions.\nAnswers:\n{answers_json}"
    )
}

/// The fields a row of a client that knows the kind has besides the plain ones.
const MARKED: [&str; 5] = [
    "version",
    "catalog",
    "catalog_version",
    "catalog_sha256",
    "element_count",
];

/// A row as a client that knows the kind is shown it in a list (`render_spec_store.summary`):
/// what the spec is, and that it was left out, as of the row's last change.
pub fn marked(row: &mut Value, artifact: &Artifact) {
    let updated_at = row.get("updated_at").cloned().unwrap_or(Value::Null);
    let Some(fields) = row.as_object_mut() else {
        return;
    };
    for key in MARKED {
        let value = artifact.payload.get(key).cloned().unwrap_or(Value::Null);
        fields.insert(key.to_string(), value);
    }
    let mut omitted = match artifact.payload.get("content_identity") {
        Some(Value::Object(identity)) => identity.clone(),
        _ => Map::new(),
    };
    omitted.insert("updated_at".to_string(), updated_at);
    fields.insert("content_omitted".to_string(), Value::Object(omitted));
}

/// The artifact itself as a client that knows the kind reads it
/// (`render_spec_store.open_current`): the spec with what it is, and once it was answered,
/// the answers.
pub fn opened(row: &mut Value, artifact: &Artifact) {
    let Some(fields) = row.as_object_mut() else {
        return;
    };
    for (key, value) in &artifact.payload {
        fields.insert(key.clone(), value.clone());
    }
    let answered = artifact.submissions.first();
    if let Some(submission) = answered.filter(|_| artifact.status == "completed") {
        fields.insert(
            "submission".to_string(),
            json!({
                "submission_id": submission.submission_id,
                "version": submission.version,
                "answers": submission.answers.clone().unwrap_or(Value::Null),
                "accepted_at": submission.created_at,
                "delivery_status": submission.status(),
            }),
        );
    }
}

/// A render spec to be published, or checked without being kept.
#[derive(Debug, Clone, PartialEq)]
pub struct Publish {
    /// The id it is to have: empty for a new one.
    pub artifact_id: String,
    pub title: String,
    pub summary: String,
    pub status: String,
    pub reference_id: String,
    /// The payload as it is kept ([`stored`]).
    pub stored: Map<String, Value>,
    /// The version the publisher says it follows: `None` when it said none.
    pub expected: Option<J>,
    /// Check it, and keep nothing.
    pub validate_only: bool,
    /// Another agent has an artifact with this id.
    pub elsewhere: bool,
    /// The trace of the turn that publishes it, as the Host proved it: kept by a new spec.
    pub source_trace_id: String,
}

impl ArtifactBook {
    /// A render spec published, or published again under its id as a whole new revision
    /// that names the version it follows (`render_spec_store.publish`, after the payload
    /// passed [`validate`]). `None` when it was only checked.
    pub fn spec_published(
        &self,
        maker: Maker<'_>,
        asked: &Publish,
        ctx: &mut Ctx<'_>,
    ) -> Result<Option<Artifact>, Refused> {
        let no = |code, path: &str| Err(Refusal::new(code, path).refused());
        if asked.elsewhere {
            return no("not_open", "");
        }
        let version = asked.stored.get("version").and_then(Value::as_str);
        let expected = asked.expected.as_ref();
        let old = Some(asked.artifact_id.as_str())
            .filter(|id| !id.is_empty())
            .and_then(|id| self.any(id));
        match old {
            Some(old) => {
                let theirs = old.kind != KIND || old.agent_id != maker.agent_id;
                if theirs || old.deleted_at.is_some() {
                    return no("not_open", "");
                }
                if old.status == "completed" {
                    return no("already_answered", "");
                }
                let current = old.payload.get("version").and_then(Value::as_str);
                let follows = expected.and_then(J::str);
                if follows != current || version == follows {
                    return no("version_mismatch", "/expected_version");
                }
            }
            None if expected.is_some() => return no("version_mismatch", "/expected_version"),
            None => {}
        }
        if !matches!(asked.status.as_str(), "ready" | "active" | "draft") {
            return no("not_open", "/status");
        }
        if asked.validate_only {
            return Ok(None);
        }
        let reference = clip(strip_hidden_blocks(&asked.reference_id).trim(), 500);
        let Some(old) = old else {
            let artifact_id = match asked.artifact_id.as_str() {
                "" => super::new_id("artifact", ctx),
                given => given.to_string(),
            };
            let new = New {
                kind: KIND.to_string(),
                title: asked.title.clone(),
                summary: asked.summary.clone(),
                status: asked.status.clone(),
                reference_id: asked.reference_id.clone(),
                source_trace_id: asked.source_trace_id.clone(),
                ..New::default()
            };
            let opening = Opening {
                kind: KIND.to_string(),
                status: asked.status.clone(),
                title: asked.title.clone(),
                artifact_id,
            };
            let mut made = self.placed(maker, &new, opening, Map::new(), ctx)?;
            made.payload = asked.stored.clone();
            return Ok(Some(made));
        };
        let same_reference = |known: &&Artifact| {
            known.artifact_id != old.artifact_id
                && known.kind == KIND
                && known.reference_id == reference
                && known.deleted_at.is_none()
        };
        if !reference.is_empty() && self.artifacts().any(|known| same_reference(&known)) {
            return Err(Refused {
                why: Why::Integrity,
                message: "UNIQUE constraint failed: artifacts.agent_id, artifacts.type, \
                          artifacts.reference_id"
                    .to_string(),
            });
        }
        Ok(Some(Artifact {
            title: clip(&asked.title, 300),
            summary: clip(strip_hidden_blocks(&asked.summary).trim(), 4_000),
            status: asked.status.clone(),
            reference_id: reference,
            payload: asked.stored.clone(),
            updated_at: ctx.now_ms.max(old.updated_at + 1),
            ..old.clone()
        }))
    }

    /// Answers to a render spec of this book (`render_spec_store.submit`, for a client that
    /// can answer): the spec as it is after, what the sender is answered, and whether
    /// anything changed. `elsewhere` is a submission under the same id in another agent's
    /// book, with the id of its artifact.
    pub fn spec_submitted(
        &self,
        catalog: &Catalog,
        artifact_id: &str,
        data: &J,
        elsewhere: Option<(&str, &Submission)>,
        now: i64,
    ) -> Result<(Artifact, Value, bool), Refused> {
        let Some(form) = self.get(artifact_id).filter(|form| form.kind == KIND) else {
            return Err(Refusal::new("not_open", "").refused());
        };
        let sid = data.get("submission_id").and_then(J::str);
        let own = sid.and_then(|sid| self.submission(sid));
        let previous = own
            .map(|(artifact, submission)| (artifact.artifact_id.as_str(), submission))
            .or(elsewhere);
        let payload = J::of(&Value::Object(form.payload.clone()));
        let before = previous.map(|(_, submission)| Previous {
            payload_hash: &submission.payload_hash,
            synthesize_audio: submission.synthesize_audio,
        });
        let admitted = admit(catalog, artifact_id, &payload, &form.status, data, before)
            .map_err(Refusal::refused)?;
        let (payload_hash, answers_json, synthesize_audio) = match admitted {
            // The same answers to the same spec: their submission is this spec's.
            Admitted::Repeat => {
                let (_, submission) = previous
                    .ok_or_else(|| Refusal::new("reused_id", "/submission_id").refused())?;
                return Ok((form.clone(), submission.receipt(artifact_id), false));
            }
            Admitted::New {
                payload_hash,
                answers_json,
                synthesize_audio,
            } => (payload_hash, answers_json, synthesize_audio),
        };
        let version = data.get("version").and_then(J::str).unwrap_or_default();
        let sid = sid.unwrap_or_default();
        let prompt = message_text(&form.title, artifact_id, version, sid, &answers_json);
        let answers = data.get("answers").map_or(Value::Null, J::to_value);
        let submission = Submission {
            submission_id: sid.to_string(),
            version: version.to_string(),
            payload_hash,
            synthesize_audio,
            created_at: now,
            prompt: Some(prompt),
            delivered_at: None,
            answers: Some(answers),
        };
        let receipt = submission.receipt(artifact_id);
        let mut form = form.clone();
        form.status = "completed".to_string();
        form.completed_at = Some(now);
        form.updated_at = now.max(form.updated_at + 1);
        form.submissions.push(submission);
        Ok((form, receipt, true))
    }
}
