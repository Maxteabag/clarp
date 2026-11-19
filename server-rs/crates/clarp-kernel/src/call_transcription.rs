//! Read-time call transcription policy. The caller supplies one settings snapshot.
use crate::{
    calls::{Refused, refused, refused_with},
    heard, languages, python,
};
use serde_json::{Value, json};

pub const LANGUAGES: &str = "transcription.call_languages";
pub const ENGINES: &str = "transcription.call_engines";

#[derive(Debug, serde::Serialize)]
pub struct Scope {
    pub principal: String,
    pub call_id: String,
    pub session: String,
}

pub fn scope(
    principal: &str,
    host: &str,
    raw_id: &str,
    raw_session: &str,
    found: Option<(&str, &Value)>,
) -> Result<Scope, Refused> {
    let id = python::strip(raw_id);
    if !id.strip_prefix("gcl_").is_some_and(|tail| {
        tail.len() == 32
            && tail
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    }) {
        return Err(refused(400, "bad call id"));
    }
    let Some((owner, call)) = found else {
        return Err(refused_with(404, "unknown call", json!({"call_id":id})));
    };
    if owner != principal || call["host_id"] != host {
        return Err(refused_with(403, "not your call", json!({"call_id":id})));
    }
    if call["state"] != "live" {
        let reason = call["reason"]
            .as_str()
            .filter(|v| !v.is_empty())
            .unwrap_or("ended");
        return Err(refused_with(
            409,
            "call ended",
            json!({"call_id":id,"reason":reason}),
        ));
    }
    let session = python::strip(raw_session);
    let participant = call["participants"]
        .as_array()
        .and_then(|ps| ps.iter().find(|p| p["session"] == session));
    if session.is_empty()
        || !participant.is_some_and(|p| matches!(p["state"].as_str(), Some("active" | "invited")))
    {
        return Err(refused_with(
            409,
            "not a call participant",
            json!({"call_id":id,"session":session}),
        ));
    }
    Ok(Scope {
        principal: owner.into(),
        call_id: id.into(),
        session: session.into(),
    })
}

pub fn concrete<'a>(model: &'a str, local: &'a str) -> &'a str {
    if matches!(model, "" | "local" | "server-default") {
        local
    } else {
        model
    }
}
fn bounded_set(model: &str) -> bool {
    matches!(model.split(':').next(), Some("google" | "elevenlabs"))
}

#[derive(Debug, serde::Serialize)]
pub struct Step {
    pub engine: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skip: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adapted: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<Option<String>>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub local_default: bool,
}

pub fn plan(order: &[String], language: Option<&str>, local: &str) -> Vec<Step> {
    order
        .iter()
        .map(|named| {
            let engine = concrete(named, local);
            let mut step = Step {
                engine: engine.into(),
                skip: None,
                language: Some(None),
                adapted: Some(false),
                local_default: matches!(named.as_str(), "" | "local" | "server-default"),
            };
            if let Some(language) = language {
                let first = language.split(',').next().unwrap_or("");
                let sent = match engine.split(':').next() {
                    Some("google") => Some(language),
                    Some("elevenlabs") => (!language.contains(',')).then_some(language),
                    _ if heard::is_cloud(engine) => Some(first),
                    _ => (engine.ends_with(".en") && first == "en").then_some("en"),
                };
                if let Some(sent) = sent {
                    step.adapted = Some(sent != language);
                    step.language = Some(Some(sent.into()));
                } else {
                    step.skip = Some("language_incompatible");
                    step.language = None;
                    step.adapted = None;
                    step.local_default = false;
                }
            }
            step
        })
        .collect()
}

pub struct Policy {
    pub language: String,
    pub engines: Vec<String>,
    pub global: String,
    pub local: String,
    pub error: String,
}
impl Policy {
    pub fn read(lang: &str, engines: &str, global: &str, local: &str) -> Self {
        let mut policy = Self {
            language: python::strip(lang).into(),
            engines: languages::engines(engines),
            global: global.into(),
            local: local.into(),
            error: String::new(),
        };
        match policy.change(Some(&json!(policy.language)), Some(&json!(engines)), global) {
            Ok((Some(l), _)) => policy.language = l,
            Err(e) => policy.error = e,
            _ => {}
        }
        policy
    }
    pub fn configured(&self) -> bool {
        !self.language.is_empty() || !self.engines.is_empty()
    }
    pub fn change(
        &self,
        language: Option<&Value>,
        engines: Option<&Value>,
        global: &str,
    ) -> Result<(Option<String>, Option<String>), String> {
        let language = language
            .filter(|v| !v.is_null())
            .map(|v| {
                let s = v
                    .as_str()
                    .ok_or("call_languages must be a string of ISO language codes")?;
                if python::strip(s).is_empty() {
                    Ok(String::new())
                } else {
                    languages::set(v)
                }
            })
            .transpose()?;
        let engines = engines
            .filter(|v| !v.is_null())
            .map(|v| {
                let s = v
                    .as_str()
                    .ok_or("call_engines must be a comma-separated string of engines")?;
                let list = languages::engines(s);
                if let Some(bad) = list.iter().find(|e| heard::cloud_model(e).is_none()) {
                    return Err(format!("unknown transcription engine: {bad}"));
                }
                Ok(list)
            })
            .transpose()?;
        let lang = language.as_deref().unwrap_or(&self.language);
        let chain = engines.as_deref().unwrap_or(&self.engines);
        if lang.contains(',') && chain.iter().any(|e| !bounded_set(e)) {
            return Err(
                "call_languages is a language set, but a call engine takes a single language"
                    .into(),
            );
        }
        let fallback = [global.to_string()];
        let usable = if chain.is_empty() { &fallback } else { chain };
        if !lang.is_empty()
            && plan(usable, Some(lang), &self.local)
                .iter()
                .all(|s| s.skip.is_some())
        {
            let names = usable
                .iter()
                .map(|e| concrete(e, &self.local))
                .collect::<Vec<_>>();
            return Err(format!(
                "call_languages {} can never work with {}",
                python::text(lang),
                names.join(", ")
            ));
        }
        Ok((language, engines.map(|v| v.join(","))))
    }
    pub fn resolve(
        &self,
        own_language: Option<&str>,
        own: &[String],
        global_language: &str,
        requested: &str,
    ) -> Resolved {
        let (lang, source) = if let Some(l) = own_language.filter(|l| !l.is_empty()) {
            (l, "agent")
        } else if !self.language.is_empty() {
            (self.language.as_str(), "call_policy")
        } else {
            (global_language, "global")
        };
        let (order, from) = if !own.is_empty() {
            (own.to_vec(), "agent")
        } else if !self.engines.is_empty() {
            (self.engines.clone(), "call_policy")
        } else if !requested.is_empty() {
            (vec![requested.into()], "device")
        } else {
            (
                vec![if heard::is_cloud(&self.global) {
                    self.global.clone()
                } else {
                    "server-default".into()
                }],
                "global",
            )
        };
        let models: Vec<_> = order.iter().map(|e| concrete(e, &self.local)).collect();
        let fields = json!({"language":{"value":lang,"source":source},"engines":{"value":models,"source":from}});
        let request=(!requested.is_empty()).then(||json!({"value":requested,"applied":from=="device","reason":(from!="device").then_some(from)}));
        Resolved {
            fields,
            requested: json!(request),
            order,
        }
    }
}
pub struct Resolved {
    pub fields: Value,
    pub requested: Value,
    pub order: Vec<String>,
}
impl Resolved {
    pub fn allows_long_model(&self) -> bool {
        matches!(
            self.fields["engines"]["source"].as_str(),
            Some("device" | "global")
        )
    }

    pub fn use_long_model(&mut self, model: &str) {
        if self.allows_long_model() && !model.is_empty() {
            self.order = vec![model.to_string()];
        }
    }

    pub fn report(&self) -> Value {
        json!({"language":self.fields["language"],"engines":self.fields["engines"],"effective_engine":null,"attempts":[],"language_adapted":null,"requested_model":self.requested})
    }
}

/// The read-time projection is absent for a closed call or a call with only global settings.
pub fn participants_view(
    policy: &Policy,
    live: bool,
    listen_first: bool,
    participants: impl Iterator<Item = (String, Resolved)>,
) -> Option<Value> {
    if !live || !policy.error.is_empty() || !(listen_first || policy.configured()) {
        return None;
    }
    let view: serde_json::Map<_, _> = participants
        .map(|(session, resolved)| (session, resolved.fields))
        .collect();
    view.values()
        .any(|v| v["engines"]["source"] != "global" || v["language"]["source"] != "global")
        .then_some(Value::Object(view))
}

/// The public report of one clip. Each attempt supplies only what its own engine observed.
pub struct Attempts {
    report: Value,
    rows: Vec<Value>,
}
impl Attempts {
    pub fn new(resolved: &Resolved) -> Self {
        Self {
            report: resolved.report(),
            rows: Vec::new(),
        }
    }
    pub fn skipped(&mut self, engine: &str, reason: &str) {
        self.rows
            .push(json!({"engine":engine,"outcome":"skipped","reason":reason}));
    }
    pub fn failed(&mut self, engine: &str, language: Option<&str>, ms: u64, reason: &str) {
        self.rows.push(json!({"engine":engine,"outcome":"error","language":language,"ms":ms,"reason":python::first(reason,200)}));
    }
    pub fn heard(&mut self, engine: &str, language: Option<&str>, ms: u64, adapted: bool) {
        self.rows
            .push(json!({"engine":engine,"outcome":"ok","language":language,"ms":ms}));
        self.report["effective_engine"] = json!(engine);
        self.report["language_adapted"] = json!(adapted.then_some("first_code"));
    }
    pub fn finish(mut self) -> Value {
        self.report["attempts"] = Value::Array(self.rows);
        self.report
    }
}
